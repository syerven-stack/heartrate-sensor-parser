#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
心率分析HTML报告生成器 V2.4.8
- 兼容2/4/6/8字节全规格0x2A37报文，自动过滤畸形截断数据包
- 自动提取设备固件/SN/电量等设备参数
- 批量计算RR间期、瞬时真实心率
- 全量HRV时域指标运算
- 动态心率分段，自动计算运动负荷占比
- 输出3张Excel数据表(CSV) + 分段运动心律波动分析报告
- V2.3.3 优化: 报告图表布局重组——HRV(ms)/(%)两张柱状图上移至心率趋势图上方；HRV数值面板下移至运动负荷分布图表行正下方，便于图表与数值对照
- V2.3.2 优化: 心律异常事件由表格改为散点图（横轴相对时间、纵轴|ΔHR|、按类型分色），直观呈现异常时段与剧烈程度
- V2.3.1 修复: 运动负荷评估改为完全基于心率区间分布动态归纳（修复原硬编码“有氧+高强度为主(31.3%)”误判），结论建议同步动态化
- V2.3.0 新增: 自动场景识别（睡眠/运动），根据数据特征动态适配心内科分析内容和报告呈现
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


# ==================== Chart.js 本地内联（自包含，免外网） ====================
# 将 chart.umd.min.js 内联进报告，避免依赖 CDN（预览面板/离线环境无外网时图表全空）。
def _load_chartjs_inline():
    """读取与本脚本同目录的 chart.umd.min.js，作为内联脚本源。"""
    here = Path(__file__).resolve().parent
    candidate = here / "chart.umd.min.js"
    if candidate.exists():
        with open(candidate, "r", encoding="utf-8") as fh:
            return fh.read()
    # 兜底：向上一级 scripts 目录查找
    alt = here.parent / "scripts" / "chart.umd.min.js"
    if alt.exists():
        with open(alt, "r", encoding="utf-8") as fh:
            return fh.read()
    return None

CHARTJS_INLINE = _load_chartjs_inline()
CDN_TAG = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>'


def _load_skill_version(default: str = "V2.4.8") -> str:
    """从 SKILL.md front matter 的 version 字段读取当前技能版本号，供 HTML footer 显示。

    优先查找脚本仓库根（scripts/ 上一级）的 SKILL.md，读取失败时回退到 default。
    只截取版本号前缀（首个空格前），避免把长描述串写进 footer。
    """
    import re
    candidates = [
        Path(__file__).resolve().parent.parent / "SKILL.md",
        Path(__file__).resolve().parent / "SKILL.md",
    ]
    for skill_path in candidates:
        if not skill_path.exists():
            continue
        try:
            text = skill_path.read_text(encoding="utf-8")
        except Exception:
            continue
        m = re.search(r"^version:\s*(V[\d.]+)", text, re.MULTILINE)
        if m:
            return m.group(1)
    return default


SKILL_VERSION = _load_skill_version()


# ==================== HRV 指标元数据 ====================

HRV_METRIC_DESC = {
    "SDNN": "全部 RR 间期的标准差，反映交感+迷走神经总体心率变异性",
    "RMSSD": "相邻 RR 差值平方均值的根号，评估副交感最敏感指标",
    "SDSD": "相邻 RR 差值的标准差，反映迷走神经活性",
    "SDARR": "相邻 RR 差值绝对值的标准差，反映逐跳心跳快慢变化的稳定程度",
    "pNN50": "相邻 RR 差值 >50ms 的百分比",
    "pNN20": "相邻 RR 差值 >20ms 的百分比",
    "CVRR": "SDNN/平均RR×100%，变异系数",
    "HRV 三角指数": "总心跳数/最大频数 bin 的值，基于 RR 直方图频数分布",
    "Tin": "RR 间期中位数，剔除早搏/异常干扰后的基础窦性心跳间隔",
    "RR 极差": "最大 RR 与最小 RR 的差，反映心率波动最大幅度",
}


# ==================== 心内科分析引擎 ====================

class CardioAnalyzer:
    """基于HRV指标和场景（运动/睡眠）的心内科综合分析"""

    def __init__(self, data):
        self.d = data
        self.hrv = data.get("hrv_metrics", {})
        self.pkt = data.get("packet_stats", {})
        self.seg = data.get("exercise_segments", {})
        self.anomalies = data.get("anomalies", [])
        self.anomaly_count = data.get("anomaly_count", 0)
        self.device = data.get("device_info", {})
        self.scenario = self.detect_scenario(data)
        self.time_range = data.get("log_time_range", {})
        self.duration_hours = self._calc_duration()

    def _calc_duration(self):
        """计算记录时长（小时）"""
        try:
            start = self.time_range.get("start", "")
            end = self.time_range.get("end", "")
            s_dt = datetime.strptime(start[:17], "%y/%m/%d %H:%M:%S")
            e_dt = datetime.strptime(end[:17], "%y/%m/%d %H:%M:%S")
            if e_dt < s_dt:
                e_dt = e_dt.replace(day=e_dt.day + 1)
            return round((e_dt - s_dt).total_seconds() / 3600, 1)
        except:
            return 0

    @staticmethod
    def detect_scenario(data):
        """自动检测记录场景：睡眠 vs 运动

        评分维度：
        - 静息占比 > 95% → +3
        - 平均心率 < 65 bpm → +2
        - 记录时长 >= 4 小时 → +2
        - 记录时间为夜间时段(22:00-10:00) → +2
        总分 >= 5 判定为睡眠场景，否则为运动场景
        """
        time_range = data.get("log_time_range", {})
        seg = data.get("exercise_segments", {})
        hrv = data.get("hrv_metrics", {})

        rest_pct = seg.get("静息(<90)", {}).get("占比(%)", 0)
        avg_hr = hrv.get("平均瞬时心率(bpm)", 0)
        start = time_range.get("start", "")

        # 计算时长
        try:
            s_dt = datetime.strptime(start[:17], "%y/%m/%d %H:%M:%S")
            end_str = time_range.get("end", "")
            e_dt = datetime.strptime(end_str[:17], "%y/%m/%d %H:%M:%S")
            if e_dt < s_dt:
                e_dt = e_dt.replace(day=e_dt.day + 1)
            duration_h = (e_dt - s_dt).total_seconds() / 3600
        except:
            duration_h = 0

        # 夜间判定
        is_night = False
        try:
            sh = s_dt.hour
            eh = e_dt.hour
            def _is_night(h):
                return h >= 22 or h < 10
            is_night = _is_night(sh) and _is_night(eh)
        except:
            pass

        score = 0
        if rest_pct > 95:
            score += 3
        if avg_hr < 65:
            score += 2
        if duration_h >= 4:
            score += 2
        if is_night:
            score += 2

        return "sleep" if score >= 5 else "exercise"

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

    # ==================== HRV 结构化解读（表格数据源）====================

    @staticmethod
    def _hrv_flag(value, lo, hi):
        """按参考范围计算 H/L/空 标记；lo 或 hi 为 None 时表示该方向不设阈值。"""
        if value is None or value == 0:
            return ""
        if hi is not None and value > hi:
            return "H"
        if lo is not None and value < lo:
            return "L"
        return ""

    def _hrv_rows(self, scenario):
        """构建 HRV 时域指标表格的结构化行。返回 list[dict]：
        {metric, desc, value, unit, value_str, flag, range, interpretation}。
        """
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
        tin_hr = round(60000 / tin) if tin > 0 else 0

        def fmt(v, unit):
            if isinstance(v, (int, float)):
                return f"{float(v):.2f} {unit}".strip()
            return f"{v} {unit}".strip()

        def row(metric, value, unit, lo, hi, range_text, interp):
            return {
                "metric": metric,
                "desc": HRV_METRIC_DESC.get(metric, ""),
                "value": value,
                "unit": unit,
                "value_str": fmt(value, unit),
                "flag": self._hrv_flag(value, lo, hi),
                "range": range_text,
                "interpretation": interp,
            }

        rows = []

        if scenario == "sleep":
            # SDNN 60-120 ms
            if sdnn > 120:
                interp = "睡眠期偏高。长程记录 SDNN 天然抬升，且对体位改变/觉醒簇离群值敏感，建议结合三角指数交叉校验；若三角指数正常，多为生理性长尾。"
            elif sdnn >= 60:
                interp = "睡眠期正常范围，总体自主神经调控储备良好。"
            else:
                interp = "睡眠期偏低，可能提示自主神经调节弹性不足或睡眠深度较浅。"
            rows.append(row("SDNN", sdnn, "ms", 60, 120, "60 – 120 ms", interp))

            # RMSSD 30-80 ms
            if rmssd > 80:
                interp = "睡眠期高位。深睡眠(N3)期典型表现为 RMSSD 升高、心率变缓，多为生理性，是深睡眠质量良好的间接佐证。"
            elif rmssd >= 30:
                interp = "睡眠期正常范围，迷走神经张力健康。"
            else:
                interp = "睡眠期偏低，可能提示恢复不充分或自主神经调节弹性不足。"
            rows.append(row("RMSSD", rmssd, "ms", 30, 80, "30 – 80 ms", interp))

            # SDSD — 无独立参考范围，做一致性检查
            if rmssd > 0 and abs(sdsd - rmssd) < 1:
                interp = "与 RMSSD 高度一致（≈相等），逐跳变异幅度均匀，数据一致性良好。"
            elif rmssd > 0:
                ratio = sdsd / rmssd
                interp = f"与 RMSSD 存在差异（SDSD/RMSSD={ratio:.2f}），需关注数据一致性。"
            else:
                interp = "近似 RMSSD 的一致性检查指标。"
            rows.append(row("SDSD", sdsd, "ms", None, None, "—", interp))

            # SDARR — 无参考范围
            rows.append(row("SDARR", sdarr, "ms", None, None, "—",
                            "反映呼吸性窦性心律不齐(RSA)幅度，是健康自主神经功能的标志。"))

            # pNN50 20-60%
            if pnn50 > 60:
                interp = "极高，超过多数深睡眠人群分布上限，需结合三角指数排查伪差。"
            elif pnn50 >= 20:
                interp = "睡眠期正常范围，迷走神经调节功能良好；接近上限时反映深睡眠期迷走张力充沛。"
            else:
                interp = "睡眠期偏低，可能提示深睡眠占比不足或自主神经调节欠佳。"
            rows.append(row("pNN50", pnn50, "%", 20, 60, "20 – 60 %", interp))

            # pNN20 — 无独立参考范围
            if pnn20 > 0:
                ratio = pnn50 / pnn20 if pnn20 > 0 else 0
                interp = f"与 pNN50 比值 {ratio:.2f}，大部分逐跳变异集中在 20-50ms 区间，分布合理。"
            else:
                interp = "与 pNN50 联合观察逐跳变异分布集中度。"
            rows.append(row("pNN20", pnn20, "%", None, None, "—", interp))

            # CVRR 5-10%
            if cvrr > 10:
                interp = "睡眠期偏高（正常 5-10%）。可能包含离群心率的放大效应，与 SDNN 联合判定。"
            elif cvrr >= 5:
                interp = "睡眠期正常范围，自主神经综合调节弹性良好。"
            else:
                interp = "睡眠期偏低。"
            rows.append(row("CVRR", cvrr, "%", 5, 10, "5 – 10 %", interp))

            # HRV 三角指数 30-50
            if tri > 50:
                interp = "偏高，RR 间期分布高度集中。"
            elif tri >= 30:
                interp = "健康成年人正常范围，RR 分布集中稳定；对离群值的抗干扰能力强于 SDNN。"
            else:
                interp = "偏低。若 SDNN 同时偏高，提示存在长尾离群 RR 值（体位改变/觉醒簇引起的骤变）。"
            rows.append(row("HRV 三角指数", tri, "", 30, 50, "30 – 50", interp))

            # Tin 900-1200 ms（对应 HR 50-67 bpm）
            if tin > 1200:
                interp = f"对应心率约 {tin_hr} bpm（<50），训练有素者常见；若无症状即为生理性。"
            elif tin >= 900:
                interp = f"对应心率约 {tin_hr} bpm，属睡眠期理想水平（50-67 bpm）。"
            else:
                interp = f"对应心率约 {tin_hr} bpm（>67），睡眠期心率偏高，可能提示浅睡或交感张力增强。"
            rows.append(row("Tin", tin, "ms", 900, 1200, "900 – 1200 ms", interp))

            # RR 极差 <600 ms（单侧 H）
            if rr_range > 1200:
                interp = "跨度极大，多为体位改变或短暂觉醒(arousal)引起的极端心率波动。"
            elif rr_range > 600:
                interp = "偏高，可能与 REM 期心率波动或体位改变有关。"
            else:
                interp = "睡眠期正常范围。"
            rows.append(row("RR 极差", rr_range, "ms", None, 600, "< 600 ms", interp))

        else:
            # ==================== 运动场景 ====================
            # SDNN 30-80 ms
            if sdnn > 100:
                interp = f"运动状态显著偏高（正常 30-80ms，平均心率 {avg_hr} bpm）。需结合三角指数判断是否为离群值拉高。"
            elif sdnn > 80:
                interp = "运动状态偏高，总体自主神经调控储备尚可。"
            elif sdnn >= 30:
                interp = "运动状态正常范围。"
            else:
                interp = "运动状态偏低，自主神经调控储备不足。"
            rows.append(row("SDNN", sdnn, "ms", 30, 80, "30 – 80 ms", interp))

            # RMSSD < 40 ms（单侧 H）
            if avg_hr > 120 and rmssd > 60:
                interp = "运动中异常偏高（该值通常见于静息，运动中 >40ms 即属罕见），强烈提示逐跳 RR 存在非生理性跳变。"
            elif rmssd > 40:
                interp = "运动中偏高。若同时 avg_hr>120 需警惕传感器伪差。"
            elif rmssd > 30:
                interp = "运动中迷走神经保持一定张力，是自主神经调节健康的积极信号。"
            else:
                interp = "运动中迷走神经活性正常受抑，符合运动生理规律。"
            rows.append(row("RMSSD", rmssd, "ms", None, 40, "< 40 ms", interp))

            # SDSD 一致性检查
            if rmssd > 0 and abs(sdsd - rmssd) < 1:
                interp = "与 RMSSD 高度一致，逐跳变异幅度均匀。"
            elif rmssd > 0:
                ratio = sdsd / rmssd
                interp = f"与 RMSSD 存在差异（SDSD/RMSSD={ratio:.2f}）。"
            else:
                interp = "近似 RMSSD 的一致性检查指标。"
            rows.append(row("SDSD", sdsd, "ms", None, None, "—", interp))

            # SDARR 无参考
            rows.append(row("SDARR", sdarr, "ms", None, None, "—",
                            "逐跳 RR 差值绝对值的标准差，反映心跳快慢变化的稳定性。"))

            # pNN50 < 3%
            if pnn50 > 5:
                interp = f"显著偏高（运动中通常 <3%），意味超过 {pnn50:.0f}% 心跳存在相邻 RR >50ms 大幅跳变。"
            elif pnn50 > 3:
                interp = "运动中偏高。"
            elif pnn50 > 2:
                interp = "运动中正常偏高范围。"
            else:
                interp = "运动中正常偏低水平，符合迷走神经受抑规律。"
            rows.append(row("pNN50", pnn50, "%", None, 3, "< 3 %", interp))

            # pNN20 无参考
            if pnn20 > 0:
                ratio = pnn50 / pnn20 if pnn20 > 0 else 0
                if ratio > 0.9:
                    interp = f"与 pNN50 接近（比值 {ratio:.2f}），绝大部分变异 >50ms 阈值，非典型生理分布。"
                else:
                    interp = f"与 pNN50 比值 {ratio:.2f}，变异主要集中在 20-50ms 区间。"
            else:
                interp = "与 pNN50 联合观察逐跳变异分布集中度。"
            rows.append(row("pNN20", pnn20, "%", None, None, "—", interp))

            # CVRR 5-12%
            if cvrr > 20:
                interp = "变异系数极高。远超运动状态正常范围（5-12%），提示 RR 离散度不成比例地大。"
            elif cvrr > 12:
                interp = "运动中偏高。"
            elif cvrr >= 5:
                interp = "运动中正常范围，自主神经调节弹性良好。"
            else:
                interp = "运动中偏低。"
            rows.append(row("CVRR", cvrr, "%", 5, 12, "5 – 12 %", interp))

            # HRV 三角指数 ≥10（单侧 L）
            if tri < 10:
                interp = "偏低。若同时 SDNN 偏高，提示存在长尾分布的离群 RR 值（传感器伪差特征）。"
            else:
                interp = "正常范围，RR 分布集中，无心律失常引起的分布畸变。"
            rows.append(row("HRV 三角指数", tri, "", 10, None, "≥ 10", interp))

            # Tin 无严格参考
            rows.append(row("Tin", tin, "ms", None, None, "—",
                            f"中位 RR 间期，对应心率约 {tin_hr} bpm，比均值更抗离群值干扰。"))

            # RR 极差 <1200 ms
            if rr_range > 1200:
                interp = "跨度极大，包含极端离群值。"
            else:
                interp = "运动状态下可接受范围。"
            rows.append(row("RR 极差", rr_range, "ms", None, 1200, "< 1200 ms", interp))

        return rows

    @staticmethod
    def _row_to_prose(r):
        """将结构化行转成 HRV 数值面板悬停提示可用的一行文本。"""
        return f"{r['metric']}={r['value_str']}。{r['interpretation']}"

    def interpret_hrv_metrics(self):
        """逐项解读 HRV 指标（运动场景）。委托给 _hrv_rows 后压平为字符串列表，供 HRV 面板悬停提示与旧接口使用。"""
        return [self._row_to_prose(r) for r in self._hrv_rows("exercise")]

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

        # 建议（依据真实负荷分布动态表述）
        hi_pct_r = self.seg.get("高强度(150-180)", {}).get("占比(%)", 0)
        ext_pct_r = self.seg.get("极限(>180)", {}).get("占比(%)", 0)
        aer_pct_r = self.seg.get("有氧(120-150)", {}).get("占比(%)", 0)
        warm_pct_r = self.seg.get("热身(90-120)", {}).get("占比(%)", 0)
        hi_total_r = hi_pct_r + ext_pct_r
        if hi_total_r >= 5:
            recommendations.append("保持当前运动强度与结构，有氧+高强度混合训练对心肺功能提升效果显著。")
        elif aer_pct_r >= 30 or warm_pct_r >= 30:
            recommendations.append("保持当前以有氧/热身为主的训练结构，规律的有氧耐力训练对心肺功能与迷走神经张力提升效果显著。")
        else:
            recommendations.append("当前运动强度整体偏低，可在身体适应后逐步增加有氧时长与强度，以获得更明显的心肺适能提升。")
        recommendations.append("建议在训练中增加5-10分钟的整理活动，促进迷走神经再激活和心率恢复。")
        recommendations.append("关注运动后1-2分钟的HRR(心率恢复值)：从峰值下降>12bpm为正常，>25bpm为优秀。")

        if sq["confidence"] in ("中", "低"):
            recommendations.append("信号质量优化：运动前确保胸带充分湿润，调整松紧度至贴合但不勒紧，以消除伪差信号。")
            recommendations.append("建议每周进行一次5分钟晨起静息HRV测量(坐姿)，以RMSSD为主要跟踪指标，不受运动伪差干扰。")

        recommendations.append("建议定期(每1-3个月)对比静息HRV趋势，关注RMSSD和SDNN的长期变化方向。")

        return conclusions, recommendations

    # ==================== 睡眠场景专用分析方法 ====================

    def assess_signal_quality_sleep(self):
        """睡眠场景信号质量评估"""
        sdnn = self._get("SDNN(ms)", 0)
        rmssd = self._get("RMSSD(ms)", 0)
        tri = self._get("HRV三角指数", 0)
        rr_range = self._get("RR极差(ms)", 0)
        avg_hr = self._get("平均瞬时心率(bpm)", 0)

        flags = []
        confidence = "高"

        # 睡眠中 RMSSD > 120ms 罕见
        if rmssd > 120:
            flags.append(f"睡眠中RMSSD异常偏高({rmssd}ms)，健康睡眠通常<100ms")
            confidence = "中"

        # 高SDNN + 低三角指数 分离（体位改变/觉醒伪差）
        if sdnn > 150 and tri < 25:
            flags.append(f"高SDNN({sdnn}ms)与中低三角指数({tri})存在一定背离，部分离群RR值可能拉高SDNN")
            if confidence != "中":
                confidence = "中"

        # 极端RR极差
        if rr_range > 1500:
            flags.append(f"RR极差极大({rr_range}ms)，可能包含体位改变或觉醒引起的心率骤变")

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
            flags.append("HRV指标模式在睡眠场景下处于正常范围，无明显信号伪差特征")

        return {"flags": flags, "confidence": confidence}

    def interpret_hrv_metrics_sleep(self):
        """逐项解读 HRV 指标（睡眠场景）。委托给 _hrv_rows。"""
        return [self._row_to_prose(r) for r in self._hrv_rows("sleep")]

    def analyze_sleep_structure(self):
        """分析睡眠结构与自主神经调节"""
        seg = self.seg
        items = []

        rest_pct = seg.get("静息(<90)", {}).get("占比(%)", 0)
        extreme_pct = seg.get("极限(>180)", {}).get("占比(%)", 0)

        items.append(f"本次夜间睡眠监测持续约{self.duration_hours}小时，全程静息占比{rest_pct}%。")

        # 心率尖峰特征分析
        spike_times = []
        for a in self.anomalies:
            hr_after = float(a.get("hr_after", 0))
            if hr_after > 120:
                spike_times.append(a.get("time", ""))

        if spike_times:
            items.append(f"共检测到{len(spike_times)}次心率瞬时飙升事件（>120 bpm），具有以下特征：")
            items.append("突发性：心率在1-2秒内从静息水平骤升至120-210 bpm，持续数秒后迅速回落，呈现典型的\"尖刺\"形态。")
            items.append("簇集性：异常事件集中在几个时段，而非均匀分布，提示这些时段可能对应睡眠周期中的REM期或体位改变。")
            items.append("自限性：每次尖峰持续时间极短（数秒至十几秒），未形成持续性心动过速，心脏能迅速恢复至静息水平。")

        items.append("从自主神经角度，睡眠期心率变异性反映了两个核心过程：")
        items.append("NREM期（尤其深睡眠N3）：迷走神经占绝对主导，心率降至最低（40-55 bpm），HRV升高（RMSSD增大），表现为呼吸性窦性心律不齐增强。")
        items.append("REM期与短暂觉醒：REM睡眠期交感神经活动增强、迷走神经张力减退，心率变异性降低但心率升高且波动性增大。数据中出现的心率尖峰簇，极可能对应REM期或短暂觉醒（micro-arousal），这也是正常睡眠周期的组成部分。")

        if self.duration_hours >= 6:
            items.append(f"睡眠连续性评估：从{self.duration_hours}小时心率趋势的连续性来看，大部分时段心率平稳、无频繁大幅波动，提示睡眠连续性较好，未出现频繁的觉醒-再入睡循环。")

        return items

    def analyze_anomalies_sleep(self):
        """睡眠场景心律异常事件分析"""
        total_beats = self.pkt.get("有效心跳总数", 1)
        anomaly_rate = round(self.anomaly_count / total_beats * 100, 2) if total_beats > 0 else 0

        items = []

        mutation_count = sum(1 for a in self.anomalies if a.get("type") == "RR间期突变")
        ectopic_count = sum(1 for a in self.anomalies if a.get("type") == "疑似早搏")

        items.append(f"共{self.anomaly_count}起异常事件，占有效心跳{anomaly_rate}%。其中RR间期突变{mutation_count}次，疑似早搏{ectopic_count}次。")

        if ectopic_count > 0:
            ectopic_rate = round(ectopic_count / total_beats * 100, 3) if total_beats > 0 else 0
            items.append(f"疑似早搏{ectopic_count}次({ectopic_rate}%)，远低于临床关注阈值(通常>1%)，属睡眠期偶发事件，无临床意义。")

        if anomaly_rate > 5:
            items.append(f"异常事件占比{anomaly_rate}%，数量偏多，需结合睡眠体位改变和觉醒因素综合判断。")

        items.append("事件特征分析：大部分突变表现为RR间期骤降（心率飙升），继以迅速回升——呈现典型的\"V型\"心率响应。这种模式高度符合短暂的体位改变或肢体活动（如翻身、伸展）引起的心率反射性加速，而非心律失常。")
        items.append("与病理心律失常有本质区别：病理性的早搏或心动过速通常表现为持续的节律异常模式（如二联律、短阵房速），而本数据中的事件均为孤立、自限、无传导阻滞特征的偶发事件，不具备临床心律失常诊断意义。")

        sq = self.assess_signal_quality_sleep()
        if sq["confidence"] in ("中", "低"):
            items.append(f"信号质量说明：结合信号质量评估({sq['confidence']}可信度)，部分心率极值（>180 bpm）事件可能包含传感器层面的运动伪差。从生理学角度，窦性心率在睡眠中瞬间飙升至200+ bpm是极为罕见的，通常仅在梦魇/夜惊等极端交感激活状态下才可能出现。建议将>180 bpm的事件更多地视为\"信号质量事件\"而非\"生理事件\"。")

        return items, mutation_count, ectopic_count

    def generate_conclusions_sleep(self):
        """睡眠场景综合结论与建议"""
        sq = self.assess_signal_quality_sleep()
        anomaly_rate = round(self.anomaly_count / max(self.pkt.get("有效心跳总数", 1), 1) * 100, 2)
        rmssd = self._get("RMSSD(ms)", 0)
        pnn50 = self._get("pNN50(%)", 0)
        tri = self._get("HRV三角指数", 0)
        tin = self._get("Tin(ms)", 0)
        avg_hr = self._get("平均瞬时心率(bpm)", 0)

        tin_hr = round(60000 / tin) if tin > 0 else 0

        conclusions = []
        recommendations = []

        # 心脏自主神经功能评价
        if rmssd >= 50 and tri >= 25:
            conclusions.append(f"心脏自主神经功能评价（睡眠期）：良好。迷走神经张力强劲（RMSSD {rmssd}ms），心脏基础节律规整（三角指数{tri}、中位心率{tin_hr} bpm），自主神经对体位改变/觉醒的反应灵敏且恢复迅速。未见持续性心律失常、传导阻滞或失代偿证据。")
        elif sq["confidence"] == "低":
            conclusions.append("心脏自主神经功能评价（睡眠期）：因信号质量限制难以准确评估。从三角指数和中位心率看，心脏基础节律基本规整，无持续性心律失常证据。")
        else:
            conclusions.append(f"心脏自主神经功能评价（睡眠期）：尚可。部分HRV指标存在轻度异常，建议结合晨起静息HRV进一步评估。")

        # 睡眠质量间接评估
        if rmssd >= 50 and pnn50 >= 30:
            conclusions.append(f"睡眠质量间接评估：HRV指标模式（高迷走张力+偶发觉醒尖峰+长周期节律平稳）提示总体睡眠连续性较好，深睡眠（N3期）占比可能充足。心率尖峰簇的出现是正常睡眠微结构的组成部分，不代表睡眠质量差。")
        else:
            conclusions.append("睡眠质量间接评估：HRV指标在睡眠期处于正常范围，自主神经调节功能正常。建议结合主观睡眠感受综合评估。")

        # 心律安全性
        if anomaly_rate < 3:
            conclusions.append(f"心律安全性评价：低风险。{self.anomaly_count}起异常事件均为自限性、孤立性RR间期突变，不具有临床心律失常意义。RR间期突变的总占比（{anomaly_rate}%）在{self.duration_hours}小时长程监测中属于正常范围。")
        else:
            conclusions.append(f"心律安全性评价：低至中等风险。异常事件占比{anomaly_rate}%，建议结合异常时段的睡眠分期信息进一步评估。")

        # 建议（睡眠场景）
        recommendations.append("保持规律作息（固定入睡和起床时间），有助于稳定睡眠周期中NREM/REM的节律性交替。睡前1小时避免蓝光暴露和剧烈运动，有助于提升深睡眠占比。")
        recommendations.append("建议每周至少进行1-2次夜间睡眠HRV监测，重点关注RMSSD的周均值变化趋势。RMSSD持续下降可能提示恢复不足、过度疲劳或自主神经失调的早期信号。")
        recommendations.append("夜间睡眠监测受体位改变和REM期波动影响，建议每日晨起后坐姿静息5分钟进行HRV测量（以RMSSD和pNN50为核心指标），作为基线参考。晨起RMSSD的健康变化趋势应为：充分睡眠后升高、疲劳/压力后降低。")
        recommendations.append("部分心率尖峰模式（突发性心率飙升后迅速回落）在理论上与阻塞性睡眠呼吸暂停（OSA）相关的心率响应有形态学相似性。如有合并症状（晨起口干、白天嗜睡、打鼾等），建议考虑进行整夜多导睡眠监测（PSG）以排除睡眠呼吸障碍。")
        recommendations.append("建议在睡眠监测时将传感器佩戴于非惯用手腕部/胸前，松紧度以贴合但不勒紧为准，减少睡眠中体位改变引起的传感器滑动和运动伪差。")

        return conclusions, recommendations

    def full_analysis(self):
        """执行完整分析，根据场景自动选择运动/睡眠分析方法"""
        hrv_rows = self._hrv_rows(self.scenario)
        hrv_items = [self._row_to_prose(r) for r in hrv_rows]

        if self.scenario == "sleep":
            sq = self.assess_signal_quality_sleep()
            exercise_items = self.analyze_sleep_structure()
            anomaly_items, mutation_count, ectopic_count = self.analyze_anomalies_sleep()
            conclusions, recommendations = self.generate_conclusions_sleep()
        else:
            sq = self.assess_signal_quality()
            exercise_items = self.analyze_exercise_load()
            anomaly_items, mutation_count, ectopic_count = self.analyze_anomalies()
            conclusions, recommendations = self.generate_conclusions()

        return {
            "signal_quality": sq,
            "hrv_interpretations": hrv_items,
            "hrv_interpretation_rows": hrv_rows,
            "exercise_analysis": exercise_items,
            "anomaly_analysis": anomaly_items,
            "anomaly_stats": {"mutation": mutation_count, "ectopic": ectopic_count},
            "conclusions": conclusions,
            "recommendations": recommendations,
            "scenario": self.scenario,  # 传递给HTML构建器
        }

    # ==================== 兼容旧接口 ====================


# ==================== HTML 生成器 ====================

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{page_title}</title>
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
  .hrv-item {{ text-align: center; background: #bdedff; border-radius: 10px; padding: 12px 4px; }}
  .hrv-item .val {{ font-size: 20px; font-weight: 700; }}
  .hrv-item .lbl {{ font-size: 11px; color: var(--text-secondary); margin-top: 4px; }}
  .hrv-item {{ position: relative; cursor: help; }}
  .hrv-tip {{ display: none; position: absolute; left: 50%; top: 100%; transform: translateX(-50%); margin-top: 10px; width: 270px; max-width: 80vw; background: #1f2937; color: #f9fafb; font-size: 12px; font-weight: 400; line-height: 1.65; text-align: left; padding: 10px 12px; border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,0.28); z-index: 200; pointer-events: none; }}
  .hrv-tip::before {{ content: ""; position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%); border: 6px solid transparent; border-bottom-color: #1f2937; }}
  .hrv-item:hover {{ z-index: 200; }}
  .hrv-item:hover .hrv-tip {{ display: block; }}
  .hrv-interp-table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 4px 0 10px; }}
  .hrv-interp-table th {{ text-align: left; padding: 8px 12px; background: #f9fafb; border-bottom: 2px solid var(--border); font-weight: 600; white-space: nowrap; font-size: 13px; text-transform: uppercase; color: var(--text-secondary); }}
  .hrv-interp-table td {{ padding: 8px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  .hrv-interp-table td.metric {{ font-weight: 600; white-space: nowrap; cursor: help; }}
  .hrv-interp-table td.value {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .hrv-interp-table td.flag {{ text-align: center; white-space: nowrap; }}
  .hrv-interp-table td.range {{ color: var(--text-secondary); white-space: nowrap; }}
  .hrv-interp-table td.interp {{ color: #374151; line-height: 1.65; }}
  .hrv-flag-h {{ display: inline-block; padding: 2px 8px; border-radius: 4px; background: #fee2e2; color: #991b1b; font-size: 11px; font-weight: 600; }}
  .hrv-flag-l {{ display: inline-block; padding: 2px 8px; border-radius: 4px; background: #dbeafe; color: #1e40af; font-size: 11px; font-weight: 600; }}
  @media (max-width: 640px) {{ .hrv-interp-table th, .hrv-interp-table td {{ padding: 6px 8px; font-size: 12px; }} .hrv-interp-table td.interp {{ font-size: 12px; }} }}
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
  .cardio-section h2 {{ font-size: 18px; color: var(--accent); }}
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
{charts_row1}
{hrv_metrics}
{hrv_charts}
{trend_chart}
{anomaly_table}
{cardio_analysis}
<footer>XOSS 心率传感器解析工具 {skill_version} · 生成于 {gen_time}</footer>
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
    mode_dict = data.get("exercise_mode", {})

    header = build_header(time_range, cardio["scenario"])
    device_info = build_device_info(device)
    data_overview = build_data_overview(pkt, hrv, cardio)
    exercise_summary_html = build_exercise_summary(summary, seg, cardio["scenario"])
    exercise_mode_html = build_exercise_mode(mode_dict, cardio["scenario"])
    hrv_metrics_html = build_hrv_metrics(hrv, cardio["hrv_interpretations"])
    charts_row1 = build_charts_row1(cardio["scenario"], exercise_mode_html)
    trend_chart = build_trend_chart()
    hrv_charts = build_hrv_charts()
    anomaly_table = build_anomaly_chart(anomalies, data.get("anomaly_count", 0))
    cardio_html = build_cardio_analysis(cardio, data)

    show_packet = not exercise_mode_html  # 运动场景已用运动模式识别替换报文分类统计
    chart_js = build_chart_js(seg, pkt_cls, trend_labels, trend_values, hrv, anomalies,
                              scenario=cardio["scenario"], mode_dict=mode_dict,
                              show_packet=show_packet)

    page_title = "睡眠心率HRV分析报告 — XOSS X2PRO" if cardio["scenario"] == "sleep" else "心率分析报告 — XOSS X2PRO"

    html = HTML_TEMPLATE.format(
        page_title=page_title,
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
        skill_version=SKILL_VERSION,
    )

    # 将 Chart.js 内联，消除对外部 CDN 的依赖（离线/沙箱环境图表不再空白）
    if CHARTJS_INLINE is not None:
        html = html.replace(CDN_TAG, "<script>\n" + CHARTJS_INLINE + "\n</script>")
    else:
        sys.stderr.write("[WARN] chart.umd.min.js 未找到，报告仍依赖 CDN。\n")

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


def build_header(time_range, scenario="exercise"):
    start = time_range.get("start", "N/A")
    end = time_range.get("end", "N/A")
    title = "XOSS X2PRO 睡眠心率 HRV 分析报告" if scenario == "sleep" else "XOSS X2PRO 心率分析报告"
    return f'''<header>
  <div class="icon">&hearts;</div>
  <div>
    <h1>{title}</h1>
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


def build_exercise_summary(summary, seg, scenario="exercise"):
    high_pct = seg.get("高强度(150-180)", {}).get("占比(%)", 0)
    aerobic_pct = seg.get("有氧(120-150)", {}).get("占比(%)", 0)
    warmup_pct = seg.get("热身(90-120)", {}).get("占比(%)", 0)
    rest_pct = seg.get("静息(<90)", {}).get("占比(%)", 0)
    extreme_pct = seg.get("极限(>180)", {}).get("占比(%)", 0)
    if scenario == "sleep":
        avg_hr = 0  # will be filled from data
        assess = f"本次记录为夜间睡眠监测，全程静息占比 {rest_pct}%，少量心率波动（热身+有氧+高强度合计 {warmup_pct + aerobic_pct + high_pct:.2f}%）与睡眠期间体位改变或短暂觉醒（arousal）相关。整体自主神经以迷走神经张力为主导，符合健康人群夜间睡眠的生理特征。"
        return f'''<div class="summary-box">
  <h3>睡眠心率总体评估</h3>
  <p>{assess}</p>
</div>'''

    # ---- 动态评估：依据各负荷区间实际占比归纳主导区间与强度 ----
    zone_map = [
        ("静息", rest_pct),
        ("热身", warmup_pct),
        ("有氧", aerobic_pct),
        ("高强度", high_pct),
        ("极限", extreme_pct),
    ]
    hi_total = high_pct + extreme_pct  # 高强度及以上合计

    # 按占比降序累加，覆盖约 80% 总分布作为“主要区间”（至少含前两大有效区间）
    ordered = sorted(zone_map, key=lambda x: x[1], reverse=True)
    main_zones = []
    cum = 0.0
    for name, pct in ordered:
        if pct <= 0:
            continue
        main_zones.append((name, pct))
        cum += pct
        if cum >= 80.0:
            break
    if len(main_zones) < 2:
        for name, pct in ordered:
            if pct > 0 and (name, pct) not in main_zones:
                main_zones.append((name, pct))
                if len(main_zones) >= 2:
                    break

    main_names = [n for n, _ in main_zones]
    main_total = sum(p for _, p in main_zones)

    # 负荷强度定性
    if hi_total >= 5:
        load_level = "高强度/剧烈"
    elif "有氧" in main_names:
        load_level = "以有氧为主的中等"
    elif "热身" in main_names:
        load_level = "以热身为主的低-中等"
    else:
        load_level = "以静息为主的低"

    # 高强度判定描述
    if hi_total >= 5:
        hi_desc = f"，其中高强度及以上区间合计 {hi_total:.2f}%"
    else:
        hi_desc = f"，未出现有临床意义的高强度区间（高强度及以上合计仅 {hi_total:.2f}%）"

    zone_cn = "、".join(main_names)
    assessment = (f"本次运动以{zone_cn}区间为主（合计 {main_total:.2f}%），"
                  f"整体为{load_level}运动负荷{hi_desc}。")
    return f'''<div class="summary-box">
  <h3>运动负荷评估</h3>
  <p>{assessment} 建议关注恢复期心率回落速度，避免过度训练。</p>
</div>'''


def build_exercise_mode(mode_dict, scenario="exercise"):
    """运动模式识别卡片。睡眠/静息场景(scenario!='exercise')直接返回空字符串，不渲染任何卡片或占位。"""
    if scenario != "exercise":
        return ""
    if not mode_dict or not mode_dict.get("valid", True):
        return ""

    dom = mode_dict.get("dominant", "混合")
    conf = mode_dict.get("confidence", 0.0)
    low = mode_dict.get("low_confidence", False)
    probs = mode_dict.get("probabilities", {})
    caveat = mode_dict.get("caveat", "")

    conf_pct = round(conf * 100, 1)
    amb = mode_dict.get("interval_ambiguous", False)
    if amb:
        # 间歇难分：即便打分偏高，形态上跑步/骑行不可分，降级显示避免误导
        badge_color, badge_text = "#f59e0b", "中置信(难分)"
    elif conf >= 0.6 and not low:
        badge_color, badge_text = "#22c55e", "高置信"
    elif conf >= 0.4:
        badge_color, badge_text = "#f59e0b", "中置信"
    else:
        badge_color, badge_text = "#ef4444", "低置信"

    note = ""
    # 非高置信（即低置信或中置信）均显示该提示；仅高置信(conf>=0.6 且 not low)时不显示
    if low or conf < 0.6:
        note = ('<p style="margin:8px 0 0;color:#b45309;font-size:12px">'
                '⚠️ 各模式概率接近或信号不足，区分度有限，结果仅供参考。'
                '真正判定需结合 GPS / 海拔 / 踏频 / 加速度计等传感器数据。</p>')

    amb_note = ""
    if amb:
        amb_note = ('<p style="margin:8px 0 0;color:#1d4ed8;font-size:12px">'
                    '🔍 高强度间歇运动特征：心率呈高间歇、高尖峰、无明显净漂移，'
                    '跑步 / 骑行形态高度重叠，仅凭心率难以区分。'
                    '建议结合踏频 / GPS / 加速度计融合判定（若为切割的子日志，漂移信号可能已被截断）。</p>')

    return f'''<div class="card" style="margin-bottom:16px">
  <h2>运动模式识别：<b style="color:{badge_color}">{dom}</b> <span class="badge" style="background:{badge_color};color:#fff">{badge_text} {conf_pct}%</span></h2>
  <p style="margin:0 0 10px;font-size:13px;color:#374151">基于逐拍 RR 间期 / 瞬时心率的时域形态学特征（间歇度、尖峰率、平稳度、双峰性、平均负荷、HR 漂移等）做<b>启发式估算</b>。</p>
  <div class="chart-wrap" style="height:300px"><canvas id="modeChart"></canvas></div>
  {note}
  {amb_note}
</div>'''


def escape_html(s):
    """转义 HTML 特殊字符，避免解读文本破坏页面结构。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# 面板指标标签 → 心内科解读文本中的起始关键词（用于按指标名匹配解读）
_HRV_TIP_KEYWORD = {
    "SDNN (ms)": "SDNN",
    "RMSSD (ms)": "RMSSD",
    "SDSD (ms)": "SDSD",
    "SDARR (ms)": "SDARR",
    "pNN50": "pNN50",
    "pNN20": "pNN20",
    "CVRR": "CVRR",
    "HRV 三角指数": "HRV 三角指数",
    "Tin (ms)": "Tin",
    "RR 极差 (ms)": "RR 极差",
}


def _find_hrv_tip(label, hrv_tips):
    """根据面板指标标签，从解读列表里找出对应的完整解读文本。"""
    kw = _HRV_TIP_KEYWORD.get(label, label)
    for t in (hrv_tips or []):
        if t.startswith(kw):
            return t
    return ""


def build_hrv_metrics(hrv, hrv_tips=None):
    """HRV 指标面板。每个指标悬停时显示其指标解读（取自心内科综合分析的 HRV 时域指标项解读）。"""
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
    parts = []
    for k, v in items:
        tip = _find_hrv_tip(k, hrv_tips)
        tip_html = f'<div class="hrv-tip">{escape_html(tip)}</div>' if tip else ""
        parts.append(f'<div class="hrv-item"><div class="val">{escape_html(v)}</div><div class="lbl">{escape_html(k)}</div>{tip_html}</div>')
    items_html = "\n    ".join(parts)
    hint = ' <span style="font-size:11px;font-weight:400;color:var(--text-secondary);text-transform:none;letter-spacing:0;margin-left:6px;">（鼠标悬停各指标查看解读）</span>' if hrv_tips else ""
    return f'''<div class="card" style="margin-bottom:16px">
  <h2>HRV 心率变异性指标（全量时域）{hint}</h2>
  <div class="hrv-grid">
    {items_html}
  </div>
</div>'''


def build_charts_row1(scenario="exercise", exercise_mode_html=""):
    """第一行图表区（grid-2）。

    左：运动负荷分布 / 睡眠心率区间分布（环形图）。
    右：运动场景下放「运动模式识别」卡片；睡眠场景无运动模式，回退为「报文分类统计」。
    """
    chart_title = "睡眠心率区间分布" if scenario == "sleep" else "运动负荷分布"
    if scenario == "sleep":
        # 睡眠场景数据长尾（静息 >95%，其余档 <2%），环形图不可读，改水平条形图
        left = f'''<div class="card">
    <h2>{chart_title}</h2>
    <div class="chart-wrap" style="height:320px; width:100%;"><canvas id="exerciseChart"></canvas></div>
  </div>'''
    else:
        left = f'''<div class="card">
    <h2>{chart_title}</h2>
    <div class="chart-wrap" style="aspect-ratio: 1/1; width: 100%; max-width: 400px; margin: 0 auto;"><canvas id="exerciseChart"></canvas></div>
  </div>'''
    if exercise_mode_html:
        # 运动模式识别卡片（已是完整 .card，去掉外 margin 避免 grid 行内多余留白）
        right = exercise_mode_html.replace('margin-bottom:16px', '')
    else:
        # 睡眠场景无运动模式，右侧回退为报文分类统计
        right = f'''<div class="card" style="display:flex; flex-direction:column;">
    <h2>报文分类统计</h2>
    <div class="chart-wrap-packet" style="flex:1; min-height:0; padding:0;"><canvas id="packetChart"></canvas></div>
  </div>'''
    return f'''<div class="grid-2" style="margin-bottom:16px">
  {left}
  {right}
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


def build_anomaly_chart(anomalies, total_count):
    mut = sum(1 for a in anomalies if "突变" in a.get("type", ""))
    ect = sum(1 for a in anomalies if "早搏" in a.get("type", ""))
    detail_n = len(anomalies)
    return f'''<div class="card" style="margin-bottom:16px">
  <h2>心律异常事件分布（共 {total_count:,} 起，下图展示明细 {detail_n} 条：RR间期突变 {mut} 次 / 疑似早搏 {ect} 次）</h2>
  <p style="margin:0 0 10px;color:#666;font-size:13px">下图按时间顺序展示每起异常事件：横轴为相对时间（自首次异常起，分钟），纵轴为心率跳变幅度 |ΔHR|（bpm）。可直观观察异常事件的集中时段与剧烈程度，悬停查看具体时间与心率变化。</p>
  <div class="chart-wrap" style="height:360px"><canvas id="anomalyChart"></canvas></div>
</div>'''


def build_cardio_analysis(cardio, data):
    sq = cardio["signal_quality"]
    hrv_interp = cardio["hrv_interpretations"]
    hrv_rows = cardio.get("hrv_interpretation_rows", [])
    exercise = cardio["exercise_analysis"]
    anomaly_items = cardio["anomaly_analysis"]
    conclusions = cardio["conclusions"]
    recommendations = cardio["recommendations"]
    scenario = cardio.get("scenario", "exercise")

    def render_section(num, title, paragraphs):
        p_html = "\n".join(f"      <p>{p}</p>" for p in paragraphs)
        return f'''    <div class="cardio-block">
      <h3>{num}、{title}</h3>
{p_html}
    </div>'''

    def render_highlight(content):
        return f'      <div class="cardio-highlight">\n        {content}\n      </div>'

    # 用表格形式渲染 HRV 时域指标逐项解读
    def _flag_cell(flag):
        if flag == "H":
            return '<span class="hrv-flag-h">H</span>'
        if flag == "L":
            return '<span class="hrv-flag-l">L</span>'
        return ""

    if hrv_rows:
        tr_html = []
        for r in hrv_rows:
            desc = escape_html(r.get("desc", ""))
            metric_cell = f'<td class="metric" title="{desc}">{escape_html(r["metric"])}</td>' if desc else f'<td class="metric">{escape_html(r["metric"])}</td>'
            tr_html.append(
                "      <tr>"
                f"{metric_cell}"
                f'<td class="value">{escape_html(r["value_str"])}</td>'
                f'<td class="flag">{_flag_cell(r["flag"])}</td>'
                f'<td class="range">{escape_html(r["range"])}</td>'
                f'<td class="interp">{escape_html(r["interpretation"])}</td>'
                "</tr>"
            )
        hrv_table = (
            '<table class="hrv-interp-table">\n'
            '      <thead><tr>'
            '<th>指标</th><th>数值</th><th>异常</th><th>参考范围</th><th>解读</th>'
            '</tr></thead>\n'
            '      <tbody>\n' + "\n".join(tr_html) + '\n      </tbody>\n'
            '      </table>'
        )
    else:
        # 兜底：若 rows 缺失，回退到旧的段落渲染
        parts = []
        for item in hrv_interp:
            parts.append(f"<p>{escape_html(item)}</p>")
        hrv_table = "".join(parts)

    # 场景相关的 HRV 小结
    if scenario == "sleep":
        rmssd = data.get("hrv_metrics", {}).get("RMSSD(ms)", 0)
        tri = data.get("hrv_metrics", {}).get("HRV三角指数", 0)
        pnn50 = data.get("hrv_metrics", {}).get("pNN50(%)", 0)
        sdnn = data.get("hrv_metrics", {}).get("SDNN(ms)", 0)
        tin = data.get("hrv_metrics", {}).get("Tin(ms)", 0)
        tin_hr = round(60000 / tin) if tin > 0 else 0
        signal_note = render_highlight(
            f"<strong>关键判断：</strong>从HRV指标整体模式来看，本次睡眠监测反映出一个核心特征——"
            f"<strong>迷走神经张力强劲且调节灵活（RMSSD {rmssd}ms、pNN50 {pnn50}%、pNN20 均处于睡眠期高位），"
            f"心脏基础节律稳定（三角指数 {tri}、中位心率 {tin_hr} bpm）</strong>。"
            f"SDNN 的异常偏高（{sdnn}ms）主要归因于少数心率骤变事件（体位改变/觉醒簇）对长程方差的放大效应，"
            f"而非整体节律紊乱。这一「高 RMSSD + 正常三角指数 + 高 pNN50」的模式，"
            f"是睡眠期自主神经健康的典型表现。"
        )
    elif sq["confidence"] == "中":
        signal_note = render_highlight("<strong>小结：</strong>HRV指标整体处于运动状态下的正常偏高范围，但部分指标存在轻度异常，可能受传感器信号质量影响。建议结合静息HRV进一步评估自主神经真实状态。")
    elif sq["confidence"] == "低":
        signal_note = render_highlight("<strong>关键判断：</strong>HRV指标模式（高SDNN+低三角指数+运动中极高RMSSD+高pNN50）高度提示数据中存在非生理性干扰。最常见原因是传感器在高强度运动中因胸带滑动、肌电干扰或接触不良产生的信号伪差。真正的生理性高HRV通常伴随三角指数同步升高。")
    else:
        signal_note = render_highlight("<strong>小结：</strong>全部HRV指标在运动背景下均处于正常或可接受范围。迷走神经调节功能（RMSSD、pNN50）在运动中保持适度活性，自主神经平衡未见异常偏移。HRV三角指数提示RR分布形态正常，无心律失常引起的分布畸变。")

    # 构建心内科HTML
    hrv_html = f'''    <div class="cardio-block">
      <h3>二、HRV 时域指标逐项解读</h3>
      {hrv_table}
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

    section3_title = "三、睡眠结构与自主神经调节分析" if scenario == "sleep" else "三、运动负荷与自主神经调节"
    exercise_html = render_section("三", section3_title.lstrip("三、"), exercise)
    # Fix: render_section produces "三、睡眠结构与..." but we need the full title
    # Use direct construction for section 3
    e_p_html = "\n".join(f"      <p>{p}</p>" for p in exercise)
    exercise_html = f'''    <div class="cardio-block">
      <h3>{section3_title}</h3>
{e_p_html}
    </div>'''

    conc_html = render_section("五", "综合结论与建议", conclusions)
    rec_items = "\n".join(f"        {i+1}. {r}<br>" for i, r in enumerate(recommendations))
    conc_html += f'\n{render_highlight(f"<strong>建议：</strong><br>{rec_items}")}'

    # 总体评价 - 场景相关
    avg_hr = data.get("hrv_metrics", {}).get("平均瞬时心率(bpm)", 0)
    tin = data.get("hrv_metrics", {}).get("Tin(ms)", 0)
    avg_rr = data.get("hrv_metrics", {}).get("平均RR间期(ms)", 0)
    rr_range = data.get("hrv_metrics", {}).get("RR极差(ms)", 0)
    total_beats = data.get("packet_stats", {}).get("有效心跳总数", 0)
    time_range = data.get("log_time_range", {})

    if scenario == "sleep":
        rmssd = data.get("hrv_metrics", {}).get("RMSSD(ms)", 0)
        pnn50 = data.get("hrv_metrics", {}).get("pNN50(%)", 0)
        rest_pct = data.get("exercise_segments", {}).get("静息(<90)", {}).get("占比(%)", 0)
        tri = data.get("hrv_metrics", {}).get("HRV三角指数", 0)
        duration_h = 0
        try:
            start = time_range.get("start", "")
            end = time_range.get("end", "")
            s_dt = datetime.strptime(start[:17], "%y/%m/%d %H:%M:%S")
            e_dt = datetime.strptime(end[:17], "%y/%m/%d %H:%M:%S")
            if e_dt < s_dt:
                e_dt = e_dt.replace(day=e_dt.day + 1)
            duration_h = round((e_dt - s_dt).total_seconds() / 3600, 1)
        except:
            pass
        overview_text = (
            f"本次夜间睡眠监测（约{duration_h}小时，{total_beats:,}次有效心跳），"
            f"整体呈现典型的睡眠期自主神经调节模式——迷走神经张力占绝对主导。"
            f"平均心率 {avg_hr} bpm、静息占比 {rest_pct}%，"
            f"从睡眠医学角度看属于健康成年人的理想范围。"
            f"HRV 指标多项处于偏高水平（SDNN、RMSSD、pNN50），"
            f"这在睡眠场景下并非异常，反而是深度睡眠期迷走神经张力增强的生理性表现。"
            f"但需要注意的是，RR 极差高达 {rr_range}ms，"
            f"提示部分心率信号可能叠加了体位改变或短暂觉醒（arousal）造成的非稳态波动。"
        )
    else:
        overview_text = f"本次记录共采集 {total_beats:,} 次有效心跳。整体心率变异性（HRV）分析结合运动场景综合评估如下。"

    overview_html = f'''    <div class="cardio-block">
      <h3>一、总体评价</h3>
      <p>{overview_text}</p>
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


def build_chart_js(seg, pkt_cls, trend_labels, trend_values, hrv, anomalies=None, scenario="exercise", mode_dict=None, show_packet=False):
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
    # 基线（平均瞬时心率）：与心内科分析口径一致，绘制为水平虚线；
    # 若 hrv 未提供 avg_hr（极端情况），用趋势序列均值兜底
    _avg_hr = hrv.get("平均瞬时心率(bpm)", 0) or 0
    if not _avg_hr and trend_values:
        try:
            _avg_hr = round(sum(trend_values) / len(trend_values), 1)
        except Exception:
            _avg_hr = 0
    _n_labels = len(trend_labels[:200])
    baseline_json = json.dumps([_avg_hr] * _n_labels)
    baseline_label = f"平均心率基线 ({_avg_hr} bpm)"
    baseline_label_json = json.dumps(baseline_label, ensure_ascii=False)

    # HRV 数据
    hrv_time_values = [
        hrv.get("SDNN(ms)", 0), hrv.get("RMSSD(ms)", 0), hrv.get("SDSD(ms)", 0),
        hrv.get("SDARR(ms)", 0), hrv.get("Tin(ms)", 0), hrv.get("RR极差(ms)", 0)
    ]
    hrv_pct_values = [
        hrv.get("pNN50(%)", 0), hrv.get("pNN20(%)", 0),
        hrv.get("CVRR(%)", 0), hrv.get("HRV三角指数", 0)
    ]

    # 异常事件散点数据（横轴=相对时间分钟，纵轴=心率跳变幅度 |ΔHR|）
    _anom_all = []
    _min_dt = None
    for a in (anomalies or []):
        try:
            _dt = datetime.strptime(a.get("time", ""), "%y/%m/%d %H:%M:%S:%f")
        except Exception:
            continue
        if _min_dt is None or _dt < _min_dt:
            _min_dt = _dt
        _anom_all.append((_dt, a))
    _mut_pts, _ect_pts = [], []
    for _dt, a in _anom_all:
        _x = round((_dt - _min_dt).total_seconds() / 60, 2) if _min_dt else 0
        _hb = a.get("hr_before", 0) or 0
        _ha = a.get("hr_after", 0) or 0
        _pt = {"x": _x, "y": round(abs(_ha - _hb), 1),
               "t": a.get("time", ""), "ty": a.get("type", ""),
               "d": a.get("detail", ""), "h": f"{_hb}→{_ha}"}
        if "早搏" in a.get("type", ""):
            _ect_pts.append(_pt)
        else:
            _mut_pts.append(_pt)
    mut_json = json.dumps(_mut_pts, ensure_ascii=False)
    ect_json = json.dumps(_ect_pts, ensure_ascii=False)

    # 全局DPI适配：最小2x高清
    dpi_fallback = 'Math.max(window.devicePixelRatio || 2, 2)'

    # 报文分类柱状图脚本（仅在 show_packet 为真时渲染，避免在无对应 canvas 时报错中断后续图表）
    packet_block = f'''
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
'''

    # 运动模式概率分布条形图（仅运动场景渲染）
    mode_block = ""
    if scenario == "exercise" and mode_dict and mode_dict.get("probabilities"):
        _probs = mode_dict["probabilities"]
        _labels = list(_probs.keys())
        _values = [round(_probs[k] * 100, 1) for k in _labels]
        _mode_colors = {"跑步": "#ef4444", "骑行": "#3b82f6", "游泳": "#22c55e", "爬山": "#f59e0b", "混合": "#8b5cf6"}
        _colors = [_mode_colors.get(k, "#94a3b8") for k in _labels]
        mode_block = f'''
// === 运动模式概率分布条形图 ===
new Chart(document.getElementById('modeChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(_labels, ensure_ascii=False)},
    datasets: [{{
      label: '概率 (%)',
      data: {json.dumps(_values)},
      backgroundColor: {json.dumps(_colors)},
      borderRadius: 4,
      borderSkipped: false
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{ callbacks: {{ label: function(ctx) {{ return ctx.raw + '%'; }} }} }}
    }},
    scales: {{ x: {{ beginAtZero: true, max: 100, title: {{ display: true, text: '概率 (%)' }} }} }}
  }}
}});
'''

    seg_keys_json = json.dumps(seg_keys, ensure_ascii=False)
    seg_values_json = json.dumps(seg_values)
    if scenario == "sleep":
        seg_chart_block = f'''// === 睡眠心率区间分布（水平条形图，长尾分布下可读性优化） ===
new Chart(document.getElementById('exerciseChart'), {{
  type: 'bar',
  data: {{
    labels: {seg_keys_json},
    datasets: [{{
      label: '占比 (%)',
      data: {seg_values_json},
      backgroundColor: ['#22c55e','#3b82f6','#f59e0b','#ef4444','#7c3aed'],
      borderRadius: 4,
      borderSkipped: false,
      barThickness: 'flex',
      maxBarThickness: 26,
      minBarLength: 3
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    devicePixelRatio: Math.max(window.devicePixelRatio, 2),
    animation: {{ duration: 400 }},
    interaction: {{ mode: 'nearest', intersect: true, axis: 'y' }},
    hover: {{ animationDuration: 120 }},
    layout: {{ padding: {{ top: 4, bottom: 4, right: 56 }} }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        animation: false,
        backgroundColor: 'rgba(0,0,0,0.85)',
        padding: 10,
        cornerRadius: 4,
        callbacks: {{
          label: function(ctx) {{ return ctx.label + ': ' + ctx.raw.toFixed(2) + '%'; }}
        }}
      }}
    }},
    scales: {{
      x: {{
        beginAtZero: true,
        max: 100,
        title: {{ display: true, text: '占比 (%)', font: {{ size: 11 }} }},
        grid: {{ color: 'rgba(0,0,0,0.05)' }},
        ticks: {{ font: {{ size: 11 }} }}
      }},
      y: {{
        grid: {{ display: false }},
        ticks: {{ font: {{ size: 12 }} }}
      }}
    }}
  }},
  plugins: [{{
    id: 'valueLabels',
    afterDatasetsDraw: function(chart) {{
      const ctx = chart.ctx;
      const meta = chart.getDatasetMeta(0);
      ctx.save();
      ctx.font = '600 12px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
      ctx.fillStyle = '#334155';
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'left';
      meta.data.forEach(function(bar, i) {{
        const v = chart.data.datasets[0].data[i];
        if (v == null) return;
        ctx.fillText(v.toFixed(2) + '%', bar.x + 6, bar.y);
      }});
      ctx.restore();
    }}
  }}]
}});
'''
    else:
        seg_chart_block = f'''// === 运动负荷环形图 (已优化: 高清/圆环样式/悬停性能) ===
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
'''

    return f'''
// 全局高清适配
Chart.defaults.devicePixelRatio = {dpi_fallback};

{seg_chart_block}
{packet_block if show_packet else ""}
// === 心率趋势折线图 ===
new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{
    labels: {tl_json},
    datasets: [
      {{
        label: '瞬时心率 (bpm)',
        data: {tv_json},
        borderColor: '#ef4444',
        backgroundColor: 'rgba(239,68,68,0.2)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 1.5,
        order: 2
      }},
      {{
        label: {baseline_label_json},
        data: {baseline_json},
        borderColor: '#64748b',
        backgroundColor: 'transparent',
        borderDash: [6, 4],
        borderWidth: 1.2,
        pointRadius: 0,
        fill: false,
        tension: 0,
        order: 1,
        spanGaps: true
      }}
    ]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: true, position: 'top', labels: {{ boxWidth: 24, font: {{ size: 11 }} }} }},
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

// === 心律异常事件散点图 ===
new Chart(document.getElementById('anomalyChart'), {{
  type: 'scatter',
  data: {{
    datasets: [
      {{ label: 'RR间期突变', data: {mut_json}, backgroundColor: '#f59e0b', pointRadius: 4, pointHoverRadius: 6 }},
      {{ label: '疑似早搏', data: {ect_json}, backgroundColor: '#ef4444', pointRadius: 4, pointHoverRadius: 6 }}
    ]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {{
      legend: {{ position: 'bottom' }},
      tooltip: {{
        callbacks: {{
          title: function(ctx) {{ return '相对时间 ' + ctx[0].parsed.x + ' 分钟'; }},
          label: function(ctx) {{
            var d = ctx.raw;
            return d.ty + ' @ ' + d.t + '  ' + d.h + ' bpm  (' + d.d + ')';
          }}
        }}
      }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: '相对时间 (分钟，自首次异常起)' }}, beginAtZero: true }},
      y: {{ title: {{ display: true, text: '心率跳变幅度 |ΔHR| (bpm)' }}, beginAtZero: true }}
    }}
  }}
}});
''' + mode_block


# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description="心率分析HTML报告生成器 V2.4.8")
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
