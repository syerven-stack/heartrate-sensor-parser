#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
运动模式细分识别（心率 / RR 时域启发式分类器）V2.0  —— 纯 Python 标准库

仅依据逐拍 RR 间期 / 瞬时心率的「时域形态学特征」，对运动场景日志估算：
    跑步 / 骑行 / 游泳 / 爬山 / 混合
的概率分布 + 置信度。

设计约束（与技能主承诺一致）：
  * 不使用 FFT / numpy，仅用标准库，避免引入第三方依赖。
  * 单凭心率无法高置信区分运动模式（各模式 HR 区间高度重叠），故定位为
    「启发式估算」而非硬判定；输出带置信度、低置信兜底(混合)与说明文案。
  * 睡眠 / 静息场景不调用本分类器做有意义判定（由上游 parse 标记 applicable=False）。

判别特征（9 个，全部由 rr_ms / inst_hr 序列 + 汇总字段计算）：
  1. intermittency  间歇指数：短时间内(<=12s) HR 下跌 >15bpm 的次数 / 10min  → 爬山
  2. spike_rate     尖峰率    ：短时间内(<=12s) HR 上涨 >15bpm 的次数 / 10min  → 爬山/间歇
  3. variability     平稳度    ：30s 滚动窗 HR 标准差的均值(bpm)，越低越稳态      → 骑行
  4. bimodality      双峰性    ：2-means 方差缩减比(0-1)，越高越双峰           → 爬山
  5. mean_load       平均负荷  ：平均瞬时心率(bpm)，偏低且平稳                  → 游泳
  6. drift           HR 漂移   ：分段均值回归斜率(bpm/h)，抗噪声、捕捉整体趋势  → 跑步；climb=会话内爬升幅度(bpm)辅助
  7. 区间分布        直接取 exercise_segments 各区间占比（透传进 features）
  8. gap_ratio       连续性    ：(有效RR报文首末跨度 - RR累加时长)/首末跨度，粗略缺口率；墙钟取RR跨度以剔除运动前后静默   → 游泳辅助
  9. drr_tail_ratio  dRR尾部比率：伪迹预筛后 P95(|dRR|)/median(|dRR|)，逐拍 RR 差分分布右尾厚重度 → 游泳/爬山上尾重、骑行/跑步平滑

附加机制（V2）：
  * 样本时长门控：duration_min<5 → 强制 low_confidence、dominant=混合（极短样本时域特征不稳定）。
  * 信号质量门控：gap_ratio>0.2 或 double_rr_rate>0.05 → 强制 low_confidence、dominant=混合，不输出具体模式。
  * 双 RR/漏搏率 double_rr_rate：RR_i≈2×邻拍(±20%) 占比，用于门控与 dRR 预筛。
"""

import math
from datetime import datetime, timedelta

# 各模式展示配色（与报告 Chart.js 对应）
MODE_COLORS = {
    "跑步": "#ef4444",
    "骑行": "#3b82f6",
    "游泳": "#22c55e",
    "爬山": "#f59e0b",
    "混合": "#8b5cf6",
}
MODE_ORDER = ["跑步", "骑行", "游泳", "爬山", "混合"]


# ==================== 基础统计 ====================
def _mean(a):
    return sum(a) / len(a) if a else 0.0


def _std(a, ddof=1):
    n = len(a)
    if n - ddof <= 0:
        return 0.0
    m = _mean(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / (n - ddof))


def _var(a):
    n = len(a)
    if n <= 1:
        return 0.0
    m = _mean(a)
    return sum((x - m) ** 2 for x in a) / (n - 1)


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _percentile(a, p):
    """线性插值百分位数，p∈[0,100]；a 为空返回 0.0。"""
    n = len(a)
    if n == 0:
        return 0.0
    s = sorted(a)
    if n == 1:
        return s[0]
    k = (n - 1) * (p / 100.0)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _softmax(d):
    keys = list(d.keys())
    vals = [d[k] for k in keys]
    m = max(vals)
    exps = [math.exp(v - m) for v in vals]
    s = sum(exps)
    return {k: e / s for k, e in zip(keys, exps)}


# ==================== 时长解析 ====================
def _parse_duration(log_time_range):
    """从 log_time_range {start,end} 解析墙钟时长(秒)。时间格式: YY/MM/DD HH:MM:SS:fff"""
    if not log_time_range:
        return 0.0
    s = (log_time_range.get("start") or "")[:17]
    e = (log_time_range.get("end") or "")[:17]
    if not s or not e:
        return 0.0
    try:
        fmt = "%y/%m/%d %H:%M:%S"
        dt0 = datetime.strptime(s, fmt)
        dt1 = datetime.strptime(e, fmt)
        if dt1 < dt0:
            dt1 += timedelta(days=1)
        return max(0.0, (dt1 - dt0).total_seconds())
    except Exception:
        return 0.0


def _parse_two_times(s, e):
    """解析两个报文时间字符串(格式 YY/MM/DD HH:MM:SS:fff)，返回间隔秒数；任一缺失/异常返回 0.0。"""
    if not s or not e:
        return 0.0
    try:
        fmt = "%y/%m/%d %H:%M:%S"
        dt0 = datetime.strptime(s[:17], fmt)
        dt1 = datetime.strptime(e[:17], fmt)
        if dt1 < dt0:
            dt1 += timedelta(days=1)
        return max(0.0, (dt1 - dt0).total_seconds())
    except Exception:
        return 0.0


# ==================== 序列处理 ====================
def _resample_1hz(inst_hr, t_sec):
    """按 rr_ms 累加时间轴将逐拍 HR 重采样为 ~1Hz 序列（同秒取均值）。"""
    if not inst_hr:
        return []
    max_t = t_sec[-1]
    nbins = int(math.ceil(max_t)) + 1
    bins = [[] for _ in range(nbins)]
    for hr, t in zip(inst_hr, t_sec):
        idx = min(int(t), nbins - 1)
        bins[idx].append(hr)
    return [_mean(b) for b in bins if b]


def _count_jumps(hr, thresh=15, window=12, direction="down"):
    """统计 1Hz HR 序列中『短窗内大幅跳变』事件次数（去抖：事件间隔>5s 才计新事件）。"""
    n = len(hr)
    if n < 2:
        return 0
    count = 0
    base = hr[0]
    base_idx = 0
    last_event = -1000
    for i in range(1, n):
        v = hr[i]
        if direction == "down":
            if v > base:
                base = v
                base_idx = i
            elif base - v >= thresh and (i - base_idx) <= window and (i - last_event) > 5:
                count += 1
                last_event = i
                base = v
        else:
            if v < base:
                base = v
                base_idx = i
            elif v - base >= thresh and (i - base_idx) <= window and (i - last_event) > 5:
                count += 1
                last_event = i
                base = v
    return count


def _rolling_std_mean(hr, win=30):
    """非重叠 30s 滚动窗 HR 标准差的均值（平稳度反向指标）。"""
    n = len(hr)
    if n < win:
        return _std(hr) if n > 1 else 0.0
    vals = []
    for i in range(0, n - win + 1, win):
        vals.append(_std(hr[i:i + win]))
    return _mean(vals)


def _rolling_std_series(hr, win=30, step=5):
    """返回 1Hz HR 的滚动窗 std 序列（每 step 秒一个点），供二阶变异 / 稳态锁定诊断。"""
    n = len(hr)
    if n < win:
        return []
    out = []
    for i in range(0, n - win + 1, step):
        w = hr[i:i + win]
        out.append(_std(w))
    return out


def _autocorr(x, lag):
    """去均值后的滞后 lag 自相关（Pearson 版本）。lag 越大数据点越少。"""
    n = len(x)
    if n <= lag + 1 or lag < 1:
        return 0.0
    m = _mean(x)
    num = sum((x[i] - m) * (x[i - lag] - m) for i in range(lag, n))
    den = sum((v - m) ** 2 for v in x)
    return num / den if den > 1e-9 else 0.0


def _zero_cross_rate(x):
    """去均值后的过零率（跨零点数 / 序列长度）。噪声型序列 ~0.5，周期型序列 <0.5。"""
    n = len(x)
    if n < 2:
        return 0.0
    m = _mean(x)
    cnt = 0
    prev = x[0] - m
    for i in range(1, n):
        cur = x[i] - m
        if (prev >= 0) != (cur >= 0):
            cnt += 1
        prev = cur
    return cnt / (n - 1)


def _bimodality_ratio(arr):
    """2-means 方差缩减比：1 - 组内方差/总方差。越接近 1 越双峰。"""
    n = len(arr)
    if n < 10:
        return 0.0
    total_var = _var(arr)
    if total_var <= 0:
        return 0.0
    s = sorted(arr)
    mid = n // 2
    g1 = s[:mid]
    g2 = s[mid:]
    w = (_var(g1) * (len(g1) - 1) + _var(g2) * (len(g2) - 1)) / (n - 2)
    return max(0.0, 1.0 - w / total_var)


def _linear_slope(hr):
    """1Hz HR 序列对时间的线性回归斜率 (bpm/h)。"""
    n = len(hr)
    if n < 3:
        return 0.0
    xs = list(range(n))
    mx = _mean(xs)
    my = _mean(hr)
    num = sum((xs[i] - mx) * (hr[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return (num / den) * 3600.0


def _segment_slope(hr, n_seg=5):
    """分段斜率：将 1Hz 序列均分为 n_seg 段取段均值，对段均值序列回归得 bpm/h。

    相比整段线性斜率，段均值平滑了秒级噪声与间歇波动，更能反映会话整体的
    上行/下行趋势（跑步典型：随运动持续爬升）。单位与整段斜率一致(bpm/h)。
    """
    n = len(hr)
    if n < n_seg * 3:
        return _linear_slope(hr)
    seg_len = n // n_seg
    means = [_mean(hr[i * seg_len:(i + 1) * seg_len]) for i in range(n_seg)]
    # 段均值序列相邻 index 间隔 = seg_len 秒；_linear_slope 内部已按 1Hz(1秒)乘过 3600，
    # 故真实 bpm/h = 段斜率 / seg_len（修正其"1 index=1秒"的过估）。
    return _linear_slope(means) / seg_len


def _climb_amplitude(hr, frac=0.33):
    """会话内爬升幅度(bpm)：后 frac 段均值 − 前 frac 段均值。

    正值表示心率随运动从低到高爬升（持续跑典型）；间歇/稳态运动通常接近 0。
    """
    n = len(hr)
    if n < 10:
        return 0.0
    k = max(1, int(n * frac))
    head = _mean(hr[:k])
    tail = _mean(hr[-k:])
    return tail - head


# ==================== 特征计算 ====================
def compute_features(rr_rows, hrv_metrics, exercise_segments, packet_stats, log_time_range):
    """从逐拍序列 + 汇总字段计算 9 个判别特征。返回 dict（含 valid 标志）。"""
    valid_rows = [
        r for r in rr_rows
        if r.get("rr_ms") and r["rr_ms"] > 0
        and r.get("inst_hr") and 30 <= r["inst_hr"] <= 220
    ]
    paired = [(r["rr_ms"], r["inst_hr"]) for r in valid_rows]
    n = len(paired)
    if n < 10:
        return {"valid": False, "reason": "有效心跳不足(<10)"}

    rr_ms = [p[0] for p in paired]
    inst_hr = [p[1] for p in paired]

    # 时间轴重建（秒）：t_i = 累加 rr_ms
    t_sec = []
    acc = 0.0
    for v in rr_ms:
        acc += v / 1000.0
        t_sec.append(acc)
    sum_rr_sec = t_sec[-1]

    # 墙钟改用「有效 RR 报文首→末跨度」，剔除运动前后的静默段 / 无 RR 收尾尾巴，
    # 避免缺口率(real_dur - sum_rr)被收尾空闲夸大（如 0701：运动后 51min 静默使缺口率虚高到 0.37）。
    # 仍保留对真实中途掉线的捕捉能力：中途长静默同样会拉大 (首末跨度 - 累加RR)。
    rr_span_sec = _parse_two_times(valid_rows[0].get("time"), valid_rows[-1].get("time"))
    real_dur_sec = rr_span_sec or _parse_duration(log_time_range) or sum_rr_sec
    dur_min = real_dur_sec / 60.0 if real_dur_sec > 0 else (sum_rr_sec / 60.0)
    per10 = (dur_min / 10.0) if dur_min > 0 else 1.0

    # 1Hz 重采样
    hr_1hz = _resample_1hz(inst_hr, t_sec)
    L = len(hr_1hz)

    # F1 / F2 间歇指数 / 尖峰率（每 10 分钟事件数）
    drops = _count_jumps(hr_1hz, thresh=15, window=12, direction="down") if L >= 2 else 0
    spikes = _count_jumps(hr_1hz, thresh=15, window=12, direction="up") if L >= 2 else 0
    intermittency = drops / per10 if per10 > 0 else 0.0
    spike_rate = spikes / per10 if per10 > 0 else 0.0

    # F3 平稳度（30s 滚动窗 std 均值）
    variability = _rolling_std_mean(hr_1hz, win=30) if L >= 2 else 0.0
    # ==================== 诊断字段（V2.4.6 尝试，不进入打分）====================
    # 目的：为骑行 vs 跑步区分寻找 HR/RR 时域上的干净分辨维度。
    # 假设：稳态骑行是"心血管系统被锁定"，序列复杂度低、二阶变异极小；
    #      跑步受呼吸/步频/地形调制，序列有短时相关性，二阶变异更大。
    #
    # D1 hr_std_of_std：30s 滚动窗 std 序列本身的 std（每 5s 步进）。稳态骑行 std 均值低且
    #    序列平坦，std_of_std 极小；跑步 std 均值高且随呼吸/步频波动，std_of_std 显著更大。
    # D2 hr_std_cv：std_of_std / variability（去掉均值幅度影响的相对稳定度）。
    # D3 rr_diff_ac1：dRR 序列滞后 1 的自相关。跑步呼吸性心律不齐(RSA)使 dRR 有短时正相关；
    #    骑行稳态 dRR 近似白噪声，ac1 ≈ 0；间歇型运动可能出现负相关。
    # D4 dhr_zcr：|ΔHR| 序列去均值后的过零率。噪声型稳态骑行 ~0.5；跑步呼吸驱动的
    #    准周期波动 ZCR 更低（能看到"堆积"结构）。
    win_stds = _rolling_std_series(hr_1hz, win=30, step=5) if L >= 35 else []
    hr_std_of_std = _std(win_stds) if len(win_stds) >= 3 else 0.0
    hr_std_cv = (hr_std_of_std / variability) if variability > 1e-3 else 0.0
    # dRR 序列（用 rr_ms 直接差分，粗预筛掉双 RR 伪迹后计算）
    _drr = [rr_ms[i] - rr_ms[i - 1] for i in range(1, len(rr_ms))]
    rr_diff_ac1 = _autocorr(_drr, 1) if len(_drr) >= 4 else 0.0
    # |ΔHR| 序列 ZCR（HR 是 1Hz 重采样后的）
    _dhr_abs = [abs(hr_1hz[i] - hr_1hz[i - 1]) for i in range(1, L)] if L >= 2 else []
    dhr_zcr = _zero_cross_rate(_dhr_abs) if len(_dhr_abs) >= 4 else 0.0


    # F4 双峰性
    bimodality = _bimodality_ratio(inst_hr)

    # F5 平均负荷
    avg_hr = hrv_metrics.get("平均瞬时心率(bpm)", _mean(inst_hr)) if isinstance(hrv_metrics, dict) else _mean(inst_hr)
    mean_load = avg_hr

    # F6 HR 漂移（V2.1：drift 改为分段斜率抗噪声；climb 为会话内爬升幅度）
    drift_raw = _linear_slope(hr_1hz) if L >= 3 else 0.0
    drift = _segment_slope(hr_1hz) if L >= 3 else 0.0
    climb = _climb_amplitude(hr_1hz)
    # 个体化归一化：以观察到的心率 P95 作为 HRmax 代理（避免单点尖峰噪声），
    # 得到 drift_pct 单位为 1/h，表示"每小时心率爬升相当于本人最大心率的多少倍"。
    # 该指标天然消除个体差异（HRmax 高的人 drift 绝对值大、HRmax 低的人 drift 小，比值相近），
    # 让"上山极端爬升"与"跑步渐进爬升"的阈值适用于不同体能水平的人群。
    hr_p95 = _percentile(hr_1hz, 95) if L >= 3 else 0.0
    drift_pct = (drift / hr_p95) if hr_p95 > 0 else 0.0

    # F7 区间分布（透传）
    seg = exercise_segments or {}
    rest_pct = seg.get("静息(<90)", {}).get("占比(%)", 0)
    warmup_pct = seg.get("热身(90-120)", {}).get("占比(%)", 0)
    aerobic_pct = seg.get("有氧(120-150)", {}).get("占比(%)", 0)
    # high_pct 语义（V2.4.3 起）：≥75% HRmax 的时间占比 = 高强度档 + 极限档合计。
    # 动态区间下（parse 按 hr_p95 按 ACSM 比例缩放），单档"高强度"会漏掉冲入极限档
    # 的跑步/爬山高强度段，故取两档并集才能对齐"持续中高强度"语义。
    high_pct_only = seg.get("高强度(150-180)", {}).get("占比(%)", 0)
    extreme_pct = seg.get("极限(>180)", {}).get("占比(%)", 0)
    high_pct = high_pct_only + extreme_pct

    # F8 连续性（墙钟 vs RR累加 缺口率，粗略）
    gap_ratio = max(0.0, (real_dur_sec - sum_rr_sec) / real_dur_sec) if real_dur_sec > 0 else 0.0

    # 双 RR / 漏搏疑似率：RR_i ≈ 2×邻拍（±20%）的比例（信号质量门控用，亦用于 dRR 伪迹预筛）
    dbl = 0
    for i in range(1, len(rr_ms) - 1):
        v, a, c = rr_ms[i], rr_ms[i - 1], rr_ms[i + 1]
        # 双 RR / 漏搏：本拍 RR 约等于邻拍的 2 倍（±20%）
        if (1.8 * a <= v <= 2.2 * a) or (1.8 * c <= v <= 2.2 * c):
            dbl += 1
    double_rr_rate = round(dbl / n, 4) if n > 0 else 0.0

    # F9 dRR 尾部比率：伪迹预筛后 |dRR| 的 P95 / 中位数（RR 差分分布右尾厚重度）
    # 预筛疑似双 RR/漏搏点，改用 P95 降敏（避免单点伪迹/极端重尾放大上尾）
    clean = [rr_ms[i] for i in range(len(rr_ms))
             if not (1 < i < len(rr_ms) - 1
                     and ((1.8 * rr_ms[i - 1] <= rr_ms[i] <= 2.2 * rr_ms[i - 1])
                          or (1.8 * rr_ms[i + 1] <= rr_ms[i] <= 2.2 * rr_ms[i + 1])))]
    drr_abs = [abs(clean[i] - clean[i - 1]) for i in range(1, len(clean))]
    if drr_abs:
        _p95 = _percentile(drr_abs, 95)
        _med = _percentile(drr_abs, 50)
        drr_tail_ratio = _p95 / _med if _med > 1e-6 else (_p95 if _p95 > 0 else 1.0)
    else:
        drr_tail_ratio = 1.0

    return {
        "valid": True,
        "n_beats": n,
        "duration_min": round(dur_min, 2),
        "intermittency": round(intermittency, 3),
        "spike_rate": round(spike_rate, 3),
        "variability": round(variability, 3),
        "bimodality": round(bimodality, 3),
        "mean_load": round(mean_load, 2),
        "drift": round(drift, 3),
        "drift_raw": round(drift_raw, 3),
        "drift_pct": round(drift_pct, 4),
        "hr_p95": round(hr_p95, 1),
        "climb": round(climb, 3),
        "rest_pct": rest_pct,
        "warmup_pct": warmup_pct,
        "aerobic_pct": aerobic_pct,
        "high_pct": high_pct,
        "extreme_pct": extreme_pct,
        "gap_ratio": round(gap_ratio, 3),
        "double_rr_rate": double_rr_rate,
        "drr_tail_ratio": round(drr_tail_ratio, 3),
        # 诊断字段（V2.4.6 试验，不进入打分公式）
        "hr_std_of_std": round(hr_std_of_std, 3),
        "hr_std_cv": round(hr_std_cv, 3),
        "rr_diff_ac1": round(rr_diff_ac1, 3),
        "dhr_zcr": round(dhr_zcr, 3),
    }


# ==================== 分类打分 ====================
def classify_mode(features):
    """对特征做加权规则打分 → softmax 概率分布 + 主导模式 + 置信度 + 兜底。"""
    if not features.get("valid", False):
        return {
            "method": "heuristic_time_domain",
            "valid": False,
            "dominant": "混合",
            "confidence": 0.0,
            "low_confidence": True,
            "probabilities": {k: (0.0 if k != "混合" else 1.0) for k in MODE_ORDER},
            "scores": {},
            "features": features,
            "caveat": "基于心率/RR时域特征的启发式估算，非传感器融合判定，置信度受限；真正判定需GPS/海拔/踏频/加速度计融合或标注数据",
        }

    # 样本时长门控：<5min 的极短样本，drift/climb/间歇等时域特征均不稳定 → 强制低置信
    dur = features.get("duration_min", 0.0)
    if dur < 5.0:
        return {
            "method": "heuristic_time_domain",
            "valid": True,
            "dominant": "混合",
            "confidence": 0.0,
            "low_confidence": True,
            "poor_signal": True,
            "signal_note": f"样本时长过短({dur:.2f}min<5min)，时域特征不稳定，无法可靠估计运动模式",
            "probabilities": {k: 0.0 for k in ["跑步", "骑行", "游泳", "爬山"]} | {"混合": 1.0},
            "scores": {},
            "features": features,
            "caveat": "样本时长过短，无法可靠估计运动模式；真正判定需更长时长记录或融合GPS/海拔/踏频/加速度计",
        }

    # 信号质量门控：缺口率或双RR率过高 → 强制低置信，不输出任何具体模式
    gap = features["gap_ratio"]
    dblr = features.get("double_rr_rate", 0.0)
    if (gap > 0.20) or (dblr > 0.05):
        return {
            "method": "heuristic_time_domain",
            "valid": True,
            "dominant": "混合",
            "confidence": 0.0,
            "low_confidence": True,
            "poor_signal": True,
            "signal_note": "信号质量不足(缺口率/双RR率过高)，无法可靠估计运动模式",
            "probabilities": {k: 0.0 for k in ["跑步", "骑行", "游泳", "爬山"]} | {"混合": 1.0},
            "scores": {},
            "features": features,
            "caveat": "信号质量不足(缺口率/双RR率过高)，无法可靠估计运动模式；真正判定需GPS/海拔/踏频/加速度计融合或标注数据",
        }

    inter = features["intermittency"]
    spike = features["spike_rate"]
    var = features["variability"]
    bim = features["bimodality"]
    load = features["mean_load"]
    drift = features["drift"]
    climb = features.get("climb", 0.0)
    gap = features["gap_ratio"]

    # 归一化到 0-1
    inter_n = _clip(inter / 8.0)
    spike_n = _clip(spike / 8.0)
    var_n = _clip(var / 15.0)
    bim_n = _clip(bim)
    load_n = _clip((load - 60.0) / 120.0)
    # drift_n 也走个体化归一化：drift_pct 从 -0.15 (每小时下降 15% HRmax) 到 0.55
    # (每小时上升 55% HRmax，即爬山信号起点)。上限与 uphill_extreme 起点接壤，
    # 语义：跑步是"从静息渐进爬向 HRmax 但达不到极端"，超过 0.55 归爬山处理。
    drift_pct = features.get("drift_pct", 0.0)
    drift_n = _clip((drift_pct + 0.15) / 0.70)
    gap_n = _clip(gap * 5.0)
    tail = features["drr_tail_ratio"]
    tail_n = 1.0 / (1.0 + math.exp(-(tail - 8.0) / 6.0))   # soft-sigmoid：tail=8→0.5，不再硬饱和

    # 爬山专属信号（个体化归一化，通过 drift_pct 消除 HRmax 差异）：
    #   uphill_extreme —— drift_pct ≥0.55 (每小时爬升 ≥55% HRmax) 起效、0.80 饱和；
    #     捕捉「地形叠加负荷」独有的极端上行速率。跑步渐进爬升实测 ≤0.52，上山样本 0.80~1.05。
    #     阈值以"占本人 HRmax 的比例"表达，对不同心率范围的人群普适；与跑步上限留 0.03 余量。
    #   downhill_signal —— 负 drift_pct 与高双峰性的乘积，捕捉上下山 session 特有的
    #     「先高后低」双峰形态；平地骑行负漂 drift_pct≈0/bim<0.4 时几乎为零。
    uphill_extreme = _clip((drift_pct - 0.55) / 0.25)
    downhill_signal = _clip((-drift_pct - 0.05) / 0.15) * bim_n

    # 跑步专属信号 sustained_high：捕捉"持续中高强度 + 高变异 + 正/近零漂移"这一跑步指纹
    #   sustained_pct：high_pct(75-90% HRmax 心率区间占比) 从 25% 到 55% 的相对位置。
    #     high_pct 已在 parse 阶段按个体 hr_p95 动态划分区间(ACSM 比例)，跨人群普适。
    #     跑步实测 43~57%（4/4），骑行 ≤27.7%（3/3），中间有干净分界带。
    #   var_running：variability(30s 窗 HR std) 从 4 到 8 的位置。稳态骑行几乎零变异
    #     (2.5~3.6)，跑步稳态段变异明显更高（5~13），作为跑步的必要条件门槛。
    #   drift_gate：软门槛拒绝"显著负漂移"的样本 —— 跑步物理特征是渐进爬升或最多轻微
    #     冷却负漂。gate = clip((drift_pct+0.20)/0.10)：drift_pct ≥ -0.10 完全放行，
    #     -0.20 完全拒绝，中间线性；实测跑步收尾段 -0.05 完全放行（保 0702-pb 冷却样本），
    #     下山段 -0.14~-0.23 大幅衰减（挡 0711_ps-2/0712_ps-2 假阳性）。
    #   tail_gate：软门槛拒绝"dRR 尾部厚重"的样本 —— 跑步逐拍 RR 差分分布平滑，
    #     P95/median 比率(drr_tail_ratio)实测多在 3~40 区间；骑行/间歇型样本因大量小 dRR
    #     叠加偶发大跳变，尾部比率飙到 50~120。gate = 1 - clip((tail-30)/30)：tail ≤30
    #     完全放行、60 完全拒绝。这是骑行 0709-qx-2 (tail=60.7) 与跑步 0702-pb (tail=3.5)
    #     唯一干净分离的维度（drift/var/high_pct 均有重叠）；tail_ratio 是"分布形态比率"，
    #     不依赖 HRmax，跨人群普适。
    #   sostd_gate（V2.4.6 新增）：软门槛拒绝"心率被锁定在窄区间"的稳态骑行/游泳。
    #     hr_std_of_std 是 30s 滚动 std 序列本身的 std（每 5s 步进），衡量"变异幅度的
    #     变异性"。稳态骑行 std 均值本身低且序列平坦，sostd 落在 1.3~3.5；跑步 std 均值
    #     高且随呼吸/地形波动，sostd 落在 5.0~10.0，中间 3.5~5.0 是干净分界带。
    #     gate = clip((sostd - 3.5) / 1.5)：sostd ≤3.5 完全拒绝、≥5.0 完全放行。
    #     这是 0709-qx-1 骑行死结的救星（drift+high+tail 全在跑步区但 sostd=2.57 挡下）。
    #     物理意义：跑步的心率被呼吸性心律不齐 + 步频扰动 + 地形起伏三重调制，30s 窗 std
    #     天然会随时间波动；骑行稳态是"心血管系统被锁定"，std 波动极小。这个维度不依赖
    #     HRmax，跨人群普适（阈值 3.5/5.0 是 bpm 的 std 的 std，本质是分布形态量）。
    #   五者相乘：任一维度不满足跑步物理特征即让 sustained_high 归零。
    high_pct = features.get("high_pct", 0.0)
    sustained_pct = _clip((high_pct - 25.0) / 30.0)
    var_running = _clip((features["variability"] - 4.0) / 4.0)
    drift_gate = _clip((drift_pct + 0.20) / 0.10)
    tail_gate = 1.0 - _clip((features["drr_tail_ratio"] - 30.0) / 30.0)
    sostd_gate = _clip((features.get("hr_std_of_std", 0.0) - 3.5) / 1.5)
    sustained_high = sustained_pct * var_running * drift_gate * tail_gate * sostd_gate

    # ==================== V2.4.7 endurance_high 耐力型高负荷通道 ====================
    # 背景：V2.4.6 sostd_gate + var_running 双门控解决了骑行死结 0709-qx-1，但同时
    # 挡住了极稳定"跑步机 / 平地体育场"类跑步样本（0714-pb: var=1.75, sostd=2.26，
    # 心率序列锁定在窄区间但 high_pct=87.4）。这类样本从时域碎波看跟稳态骑行没差，
    # 但 high_pct 87% 远超骑行天花板 46%——用宏观负荷占比作为独立救回通道。
    #
    # 数据支撑（20 样本）：
    #   跑步 high_pct ∈ [51.5, 87.4]（全部 ≥50%）
    #   骑行 high_pct ∈ [1.4, 45.9]（天花板 45.9，中位 22%）
    #   爬山 high_pct ∈ [45.9, 79.9]（与跑步共享此特征，故爬山也吃一份）
    # 50~80% 是干净分界带，跑步/爬山共同的"耐力型陆上运动"指纹。
    #
    # 物理意义：骑行受限于坐姿 + 踏频 + 车辆阻力，很难持续 ≥75% HRmax；跑步/爬山靠
    # 全身肌群 + 支撑体重，容易长时间维持在有氧-高强度交界；同时 drift_pct ≥ -0.05
    # 排除深度冷却负漂样本（虽 0702-pb -0.051 差点被挡，仍能通过其他通道判跑步）。
    #
    # 大众适用性：high_pct 是"≥75% HRmax 占比"、drift_pct 是"占 HRmax 比例"，
    # 全是形态量/相对量，不涉及 bpm 绝对阈值，跨人群普适。
    endurance_pct = _clip((high_pct - 50.0) / 30.0)
    drift_nonneg = _clip((drift_pct + 0.05) / 0.15)
    endurance_high = endurance_pct * drift_nonneg

    # 加权打分（V2.4.3 重标定 + V2.4.6 sostd 扩展 + V2.4.7 endurance 补丁）
    # struct_n（间歇+尖峰+双峰）跑步/骑行/爬山平权，不再厚此薄彼；
    # 骑行由中高负荷主导，跑步由 drift + 持续高强度双主导，
    # 爬山由地形专属的 uphill/downhill 信号主导，其余项作背景支撑。
    #
    # V2.4.6：drift_n 通道加 sostd_gate。物理意义——只有当 30s 滚动 std 序列本身也在
    # 波动（sostd ≥3.5）时，正漂才应被解读为"跑步渐进升温"。稳态骑行也可能因为热身/
    # 爬坡出现正漂（0709-qx-1 drift_pct=0.463 却 sostd=2.57），此时不能让 drift 通道
    # 单独把它推向跑步。爬山有自己的 uphill_extreme 通道（不共用 drift_n），互不干扰；
    # 真跑步的 sostd 都 ≥5.05，gate=1 完全放行，不影响原有跑步识别。
    struct_n = inter_n + spike_n + bim_n
    running_specific = 2.5 * drift_n * sostd_gate + 1.5 * sustained_high + 1.5 * endurance_high
    scores = {
        "爬山": 1.0 * struct_n + 0.8 * var_n + 0.8 * tail_n
                + 5.0 * uphill_extreme + 3.0 * downhill_signal + 1.0 * endurance_high,
        "骑行": 1.0 * struct_n + 2.0 * load_n + 0.6 * var_n + 0.6 * tail_n,
        "跑步": 1.0 * struct_n + 1.5 * load_n + running_specific + 0.4 * tail_n,
        "游泳": 0.5 * struct_n + 0.5 * (1 - load_n) + 1.0 * (1 - bim_n) + 0.6 * gap_n + 0.6 * tail_n,
    }

    probs4 = _softmax(scores)
    items = sorted(probs4.items(), key=lambda x: x[1], reverse=True)
    p_max = items[0][1]
    p_2nd = items[1][1]
    margin = p_max - p_2nd

    # 低置信判定：竞争接近 或 最高概率偏低（V2 放宽，骑行/爬山/游泳三强接近时诚实降级）
    low_conf = (margin < 0.12) or (p_max < 0.34)

    # 跑步/骑行（仅靠 HR 极难区分）同时进入前二且彼此接近时，整体降为低置信 → 兜底为混合
    # 注意：仅当「前二」均为 跑步/骑行 时才触发，避免误伤明显主导的 爬山/游泳 等模式
    top2 = sorted(probs4.items(), key=lambda x: x[1], reverse=True)[:2]
    top2_modes = {m for m, _ in top2}
    if top2_modes <= {"跑步", "骑行"} and (top2[0][1] - top2[1][1]) < 0.15:
        low_conf = True

    other_frac = 0.45 if low_conf else 0.0
    if low_conf:
        probs4 = {k: v * (1 - other_frac) for k, v in probs4.items()}

    probabilities = {k: round(probs4.get(k, 0.0), 4) for k in ["跑步", "骑行", "游泳", "爬山"]}
    probabilities["混合"] = round(other_frac, 4)

    dominant = "混合" if low_conf else items[0][0]
    confidence = max(probabilities.values())

    # 间歇运动难分标记：高间歇 + 高尖峰 + 无明显漂移 → 跑步/骑行仅凭心率难以区分
    interval_ambiguous = bool(inter >= 12 and spike >= 12 and abs(drift) < 8)

    return {
        "method": "heuristic_time_domain",
        "valid": True,
        "dominant": dominant,
        "confidence": round(confidence, 4),
        "low_confidence": bool(low_conf),
        "interval_ambiguous": interval_ambiguous,
        "margin": round(margin, 4),
        "probabilities": probabilities,
        "scores": {k: round(v, 3) for k, v in scores.items()},
        "features": features,
        "caveat": "基于心率/RR时域特征的启发式估算，非传感器融合判定，置信度受限；真正判定需GPS/海拔/踏频/加速度计融合或标注数据",
    }


# ==================== 对外入口（供 parse_heart_rate_log.py 调用） ====================
def classify_exercise_mode(rr_rows, hrv_metrics, exercise_segments, packet_stats, log_time_range):
    """镜像 CardioAnalyzer.detect_scenario 的调用风格；直接消费解析阶段内存中的逐拍数据。"""
    feats = compute_features(rr_rows, hrv_metrics, exercise_segments, packet_stats, log_time_range)
    return classify_mode(feats)


if __name__ == "__main__":
    # 简单自测：从 JSON + 心跳明细 CSV 复算（CSV 缺逐拍时间戳，故用 rr_ms 累加重建）
    import json
    import csv
    import sys

    if len(sys.argv) < 3:
        print("用法: python classify_exercise_mode.py 分析结果.json 心跳明细.csv")
        sys.exit(1)
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    rows = []
    with open(sys.argv[2], encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({"rr_ms": float(r["rr_ms"]), "inst_hr": float(r["inst_hr"])})
            except Exception:
                continue
    res = classify_exercise_mode(
        rows,
        data.get("hrv_metrics", {}),
        data.get("exercise_segments", {}),
        data.get("packet_stats", {}),
        data.get("log_time_range", {}),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
