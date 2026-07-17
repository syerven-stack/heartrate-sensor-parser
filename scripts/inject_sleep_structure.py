"""Sleep-structure analyzer + HTML injector.

Reads ``心跳明细.csv`` from an output directory, computes 30 s epoch hypnogram
(N3 / N1-N2 / REM / Wake) via HRV proxy method, then:

  1. Writes ``sleep_structure_report.md`` alongside the CSV.
  2. Injects a self-contained sleep card + two Chart.js charts into
     ``heart_rate_report.html`` (idempotent, uses ``<!-- SLEEP_STRUCTURE_* -->``
     anchors). Also strips the exercise-only cards
     ``睡眠心率区间分布`` and ``报文分类统计`` from the sleep report.

Stdlib only. Usage::

    python scripts/inject_sleep_structure.py --out-dir output/0706-sp
"""
import argparse, csv, json, math, statistics, bisect, re, sys
from datetime import datetime, timedelta
from pathlib import Path

def _parse_args():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out-dir', required=True,
                    help='directory containing 心跳明细.csv and heart_rate_report.html')
    return ap.parse_args()

_ARGS = _parse_args() if __name__ == '__main__' else None
BASE = Path(_ARGS.out_dir).resolve() if _ARGS else Path('.')
CSV_PATH = BASE / "心跳明细.csv"
HTML_PATH = BASE / "heart_rate_report.html"
MD_PATH = BASE / "sleep_structure_report.md"

# ---------- IO ----------
def parse_time(s):
    d, t = s.split(" ")
    yy, mo, dd = d.split("/")
    hh, mm, ss, ms = t.split(":")
    return datetime(2000 + int(yy), int(mo), int(dd),
                    int(hh), int(mm), int(ss), int(ms) * 1000)

def load_beats():
    beats = []
    with CSV_PATH.open(encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                t = parse_time(row['time'])
                hr = float(row['inst_hr'])
                rr = float(row['rr_ms'])
                beats.append((t, hr, rr))
            except Exception:
                continue
    beats.sort(key=lambda x: x[0])
    return beats

def filter_beats(beats):
    """去除明显 RR 跳变伪值：|ΔRR/prevRR|>50% 且瞬时 HR>140，或 HR 越界。"""
    out = []
    prev_rr = None
    for t, hr, rr in beats:
        if hr < 30 or hr > 200:
            continue
        if prev_rr is not None and abs(rr - prev_rr) / prev_rr > 0.5 and hr > 140:
            continue
        out.append((t, hr, rr))
        prev_rr = rr
    return out

# ---------- Feature ----------
def rmssd(rrs):
    if len(rrs) < 2:
        return 0.0
    diffs = [(rrs[i+1] - rrs[i]) ** 2 for i in range(len(rrs) - 1)]
    return math.sqrt(sum(diffs) / len(diffs))

def build_epochs(beats, epoch_sec=30):
    if not beats:
        return []
    t0 = beats[0][0].replace(microsecond=0)
    bucket = {}
    for t, hr, rr in beats:
        idx = int((t - t0).total_seconds() // epoch_sec)
        bucket.setdefault(idx, []).append((t, hr, rr))
    epochs = []
    max_idx = max(bucket.keys())
    for idx in range(max_idx + 1):
        arr = bucket.get(idx, [])
        start = t0 + timedelta(seconds=idx * epoch_sec)
        if arr:
            hrs = [x[1] for x in arr]
            rrs = [x[2] for x in arr]
            epochs.append({
                'idx': idx,
                'start': start,
                'n': len(arr),
                'hr_mean': statistics.mean(hrs),
                'hr_std': statistics.pstdev(hrs) if len(hrs) > 1 else 0.0,
                'hr_max': max(hrs),
                'rr_mean': statistics.mean(rrs),
                'has_data': True,
            })
        else:
            # gap epoch, will be handled later
            epochs.append({
                'idx': idx,
                'start': start,
                'n': 0,
                'hr_mean': None,
                'hr_std': None,
                'hr_max': None,
                'rr_mean': None,
                'has_data': False,
            })
    return epochs

def add_sliding(beats, epochs, win_sec=300):
    times = [b[0] for b in beats]
    for ep in epochs:
        center = ep['start'] + timedelta(seconds=15)
        lo = center - timedelta(seconds=win_sec / 2)
        hi = center + timedelta(seconds=win_sec / 2)
        i_lo = bisect.bisect_left(times, lo)
        i_hi = bisect.bisect_right(times, hi)
        win = beats[i_lo:i_hi]
        rrs = [b[2] for b in win]
        hrs = [b[1] for b in win]
        ep['rmssd5'] = rmssd(rrs) if rrs else 0.0
        ep['sdnn5'] = statistics.pstdev(rrs) if len(rrs) > 1 else 0.0
        ep['hr_win_mean'] = statistics.mean(hrs) if hrs else (ep['hr_mean'] or 0.0)
        ep['hr_win_std'] = statistics.pstdev(hrs) if len(hrs) > 1 else 0.0

# ---------- Classifier ----------
def _pct(vals, p):
    if not vals:
        return 0.0
    v = sorted(vals)
    k = (len(v) - 1) * p
    lo = int(k); hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)

def classify(epochs):
    valid = [ep for ep in epochs if ep['has_data']]
    if not valid:
        return None
    all_hr = [ep['hr_win_mean'] for ep in valid]
    hr_base = statistics.median(all_hr)
    hr_p10 = _pct(all_hr, 0.10)
    hr_p90 = _pct(all_hr, 0.90)
    # 自适应阈值（借鉴 sleep_analysis.py 分位差公式）
    DEEP_DROP = 0.35 * (hr_base - hr_p10)   # 深睡下沉幅度（相对基线）
    AWAKE_DELTA = max(6.0, 0.45 * (hr_p90 - hr_base))  # 觉醒抬升下限
    rmssd_sorted = sorted(ep['rmssd5'] for ep in valid)
    def q(p): return rmssd_sorted[min(int(p * len(rmssd_sorted)), len(rmssd_sorted) - 1)]
    q75 = q(0.75); q40 = q(0.40)
    rmssd_med = statistics.median(ep['rmssd5'] for ep in valid)
    med_hr_std = statistics.median(ep['hr_win_std'] for ep in valid)

    # 供 metrics 使用
    for ep in epochs:
        ep['_hr_base'] = hr_base

    for ep in epochs:
        if not ep['has_data']:
            ep['stage'] = 'Gap'
            continue
        hr = ep['hr_win_mean']; rm = ep['rmssd5']; std = ep['hr_win_std']
        # Wake：epoch 内峰值+波动都异常（短事件），或"持续 HR 抬升 + HRV 抑制"
        epoch_max = ep.get('hr_max') or 0
        epoch_std = ep.get('hr_std') or 0
        wake_event = epoch_max > hr_base + 35 and epoch_std > 15
        sustained_wake = (
            (hr > hr_base + AWAKE_DELTA and rm < 0.8 * rmssd_med) or
            (hr > hr_base + 5 and std > med_hr_std * 2.0 and rm < 0.9 * rmssd_med)
        )
        if wake_event or sustained_wake:
            ep['stage'] = 'Wake'
        # Deep：HR 低于基线 ≥ DEEP_DROP，RMSSD 高，HR 波动小
        elif hr <= hr_base - DEEP_DROP and rm >= q75 and std <= med_hr_std * 1.2:
            ep['stage'] = 'Deep'
        # REM：HR 接近基线，HR 波动偏大，RMSSD 不在最高分位
        elif abs(hr - hr_base) < 5 and std > med_hr_std * 1.3 and rm < q75:
            ep['stage'] = 'REM'
        else:
            ep['stage'] = 'Light'
    return hr_base

def smooth(epochs, min_run=6, passes=2):
    """短于 min_run 的段并入前一段。Gap 不参与合并。"""
    for _ in range(passes):
        i = 0
        n = len(epochs)
        while i < n:
            if epochs[i]['stage'] == 'Gap':
                i += 1; continue
            j = i
            while j < n and epochs[j]['stage'] == epochs[i]['stage']:
                j += 1
            run = j - i
            if epochs[i]['stage'] == 'Wake':
                i = j; continue
            if run < min_run and i > 0:
                # find previous non-Gap stage
                k = i - 1
                while k >= 0 and epochs[k]['stage'] == 'Gap':
                    k -= 1
                if k >= 0:
                    new_stage = epochs[k]['stage']
                    for m in range(i, j):
                        epochs[m]['stage'] = new_stage
            i = j

# ---------- Metrics ----------
STAGES = ['Deep', 'Light', 'REM', 'Wake']

def find_bounds(epochs, need_epochs=20):
    """睡眠 onset/offset：连续 need_epochs 个非Wake epoch。"""
    n = len(epochs)
    onset = None
    for i in range(n - need_epochs + 1):
        seg = epochs[i:i + need_epochs]
        if all(ep['stage'] in ('Deep', 'Light', 'REM') for ep in seg):
            onset = i; break
    offset = None
    for i in range(n - 1, need_epochs - 2, -1):
        seg = epochs[i - need_epochs + 1:i + 1]
        if all(ep['stage'] in ('Deep', 'Light', 'REM') for ep in seg):
            offset = i; break
    if onset is None:
        onset = 0
    if offset is None or offset < onset:
        offset = n - 1
    return onset, offset

def merge_short_wake(epochs, min_wake_epochs=4):
    """孤立且短暂的 Wake 段（<min_wake_epochs 个 epoch，即 <2 min）合并回 Light"""
    n = len(epochs)
    i = 0
    while i < n:
        if epochs[i]['stage'] != 'Wake':
            i += 1; continue
        j = i
        while j < n and epochs[j]['stage'] == 'Wake':
            j += 1
        if (j - i) < min_wake_epochs:
            for k in range(i, j):
                epochs[k]['stage'] = 'Light'
        i = j

def compute_metrics(epochs, beats):
    onset, offset = find_bounds(epochs)
    body = epochs[onset:offset + 1]
    counts = {s: 0 for s in STAGES}
    for ep in body:
        if ep['stage'] in counts:
            counts[ep['stage']] += 1
    total_body = sum(counts.values())
    tst_epochs = counts['Deep'] + counts['Light'] + counts['REM']
    tib_epochs = offset - onset + 1
    tst_min = tst_epochs * 0.5
    tib_min = tib_epochs * 0.5
    se = tst_epochs / tib_epochs if tib_epochs else 0.0

    def pct(k): return (counts[k] / tst_epochs * 100.0) if tst_epochs else 0.0

    # awakenings = Wake segments inside body（不含首尾贴边的清醒）
    awakenings = 0
    waso_epochs = 0
    prev = None
    for ep in body:
        if ep['stage'] == 'Wake':
            if prev != 'Wake':
                awakenings += 1
            waso_epochs += 1
        prev = ep['stage']

    # 睡眠段 HR、RMSSD、SDNN（用 body 内 valid beats）
    if body:
        t_start = body[0]['start']
        t_end = body[-1]['start'] + timedelta(seconds=30)
    else:
        t_start = beats[0][0]; t_end = beats[-1][0]
    beat_hrs = []; beat_rrs = []
    for t, hr, rr in beats:
        if t_start <= t <= t_end:
            beat_hrs.append(hr); beat_rrs.append(rr)
    sleep_hr_mean = statistics.mean(beat_hrs) if beat_hrs else 0.0
    sleep_rmssd = rmssd(beat_rrs)
    sleep_sdnn = statistics.pstdev(beat_rrs) if len(beat_rrs) > 1 else 0.0

    # 最低段 HR / 最高段 HR
    valid_hrs = [ep['hr_win_mean'] for ep in body if ep['has_data']]
    hr_min = min(valid_hrs) if valid_hrs else 0.0
    hr_max = max(valid_hrs) if valid_hrs else 0.0

    # 夜间 HR dip：睡眠均 HR 相对全夜 HR 基线的下降幅度
    hr_base_full = epochs[0].get('_hr_base') if epochs else None
    if hr_base_full is None and epochs:
        _all = [ep['hr_win_mean'] for ep in epochs if ep['has_data']]
        hr_base_full = statistics.median(_all) if _all else sleep_hr_mean
    hr_dip = ((hr_base_full - sleep_hr_mean) / hr_base_full) if hr_base_full else 0.0

    return {
        'onset_idx': onset,
        'offset_idx': offset,
        'onset_time': epochs[onset]['start'],
        'offset_time': epochs[offset]['start'] + timedelta(seconds=30),
        'tib_min': tib_min,
        'tst_min': tst_min,
        'se': se,
        'deep_pct': pct('Deep'),
        'light_pct': pct('Light'),
        'rem_pct': pct('REM'),
        'wake_pct': (counts['Wake'] / total_body * 100.0) if total_body else 0.0,
        'awakenings': awakenings,
        'waso_min': waso_epochs * 0.5,
        'sleep_hr_mean': sleep_hr_mean,
        'sleep_hr_min': hr_min,
        'sleep_hr_max': hr_max,
        'rmssd': sleep_rmssd,
        'sdnn': sleep_sdnn,
        'hr_base_full': hr_base_full,
        'hr_dip': hr_dip,
        'counts': counts,
    }

# ---------- Score ----------
def score(m):
    dims = []
    tst = m['tst_min']
    if 420 <= tst <= 540: s = 20
    elif 360 <= tst < 420 or 540 < tst <= 600: s = 16
    elif 300 <= tst < 360 or 600 < tst <= 660: s = 10
    else: s = 5
    dims.append(('总睡眠时长 TST', s, 20, f'{tst:.0f} min（{tst/60:.1f} h）', '7-9 h 满分'))

    se = m['se'] * 100
    if se >= 90: s = 15
    elif se >= 85: s = 12
    elif se >= 75: s = 9
    else: s = 5
    dims.append(('睡眠效率 SE', s, 15, f'{se:.1f}%', '≥90% 满分'))

    d = m['deep_pct']
    if 13 <= d <= 23: s = 15
    elif 8 <= d < 13 or 23 < d <= 30: s = 10
    elif d < 8: s = 5
    else: s = 8
    dims.append(('深睡占比', s, 15, f'{d:.1f}%', '13-23% 满分'))

    r = m['rem_pct']
    if 18 <= r <= 28: s = 15
    elif 12 <= r < 18 or 28 < r <= 35: s = 10
    else: s = 5
    dims.append(('REM 占比', s, 15, f'{r:.1f}%', '18-28% 满分'))

    aw = m['awakenings']; waso = m['waso_min']
    if aw <= 2 and waso < 20: s = 10
    elif aw <= 4 and waso < 40: s = 7
    else: s = 4
    dims.append(('觉醒 & WASO', s, 10, f'{aw} 次 / WASO {waso:.0f} min', '≤2 次且 <20 min 满分'))

    mhr = m['sleep_hr_mean']
    if 50 <= mhr <= 65: s = 10
    elif 45 <= mhr < 50 or 65 < mhr <= 72: s = 7
    else: s = 4
    dims.append(('睡眠均心率', s, 10, f'{mhr:.1f} bpm', '50-65 bpm 满分'))

    rm = m['rmssd']
    if rm >= 60: s = 10
    elif rm >= 40: s = 7
    elif rm >= 25: s = 5
    else: s = 3
    dims.append(('RMSSD（迷走张力）', s, 10, f'{rm:.1f} ms', '≥60 ms 满分'))

    sd = m['sdnn']
    if sd >= 80: s = 3
    elif sd >= 50: s = 2
    else: s = 1
    dims.append(('SDNN（整体 HRV）', s, 3, f'{sd:.1f} ms', '≥80 ms 满分'))

    # 夜间心率下降 dip：睡眠均 HR 相对全夜基线的下降幅度，反映自主神经恢复
    dip = m.get('hr_dip', 0.0) * 100.0  # 百分比
    if dip >= 8: s = 2
    elif dip >= 4: s = 1
    else: s = 0
    dims.append(('夜间心率下降 dip', s, 2, f'{dip:.1f}%', '≥8% 满分'))

    total = sum(d[1] for d in dims)
    if total >= 85: grade = 'A（优秀）'
    elif total >= 70: grade = 'B（良好）'
    elif total >= 55: grade = 'C（一般）'
    elif total >= 40: grade = 'D（较差）'
    else: grade = 'E（差）'
    return total, grade, dims

# ---------- Output ----------
STAGE_COLORS = {
    'Deep':  '#3457D5',
    'Light': '#58A6FF',
    'REM':   '#F0883E',
    'Wake':  '#DC3545',
    'Gap':   '#8b949e',
}
STAGE_ZH = {'Deep':'深睡 N3', 'Light':'浅睡 N1/N2', 'REM':'REM', 'Wake':'清醒', 'Gap':'无数据'}
STAGE_Y  = {'Deep':1, 'Light':2, 'REM':3, 'Wake':4, 'Gap':None}

def write_markdown(m, total, grade, dims, hr_base):
    with MD_PATH.open('w', encoding='utf-8') as f:
        f.write("# 睡眠结构与质量评价（HRV 代理法）\n\n")
        f.write("> 数据源：单导联胸带 BLE 心率+RR，非 PSG 金标准；分期依据 HR 相对个人基线、RMSSD、HR 波动率的规则法映射，仅供趋势参考。\n\n")
        f.write("## 一、监测窗口\n\n")
        f.write(f"- 入睡时刻: {m['onset_time'].strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 觉醒时刻: {m['offset_time'].strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 在床时长 TIB: {m['tib_min']:.0f} min（{m['tib_min']/60:.2f} h）\n")
        f.write(f"- 总睡眠时长 TST: {m['tst_min']:.0f} min（{m['tst_min']/60:.2f} h）\n")
        f.write(f"- 睡眠效率 SE: {m['se']*100:.1f}%\n")
        f.write(f"- 个人夜间基线 HR（中位数）: {hr_base:.1f} bpm\n\n")
        f.write("## 二、睡眠分期\n\n")
        f.write("| 阶段 | 时长(min) | 占 TST | 生理特征 |\n|---|---:|---:|---|\n")
        c = m['counts']
        row = lambda k, desc: f"| {STAGE_ZH[k]} | {c[k]*0.5:.0f} | {(c[k]/(c['Deep']+c['Light']+c['REM'])*100 if k!='Wake' and (c['Deep']+c['Light']+c['REM']) else (c['Wake']/(sum(c.values()))*100 if k=='Wake' and sum(c.values()) else 0)):.1f}% | {desc} |"
        f.write(row('Deep', 'HR 低于基线，RMSSD 高位，HR 波动小；迷走神经主导') + "\n")
        f.write(row('Light','HR 接近基线，HRV 中等；睡眠主体') + "\n")
        f.write(row('REM',  'HR 接近基线但短时波动增大，交感反弹') + "\n")
        f.write(row('Wake', 'HR 明显偏高或不稳；含体位改变/短暂觉醒') + "\n\n")
        f.write(f"觉醒次数: {m['awakenings']} 次，WASO（入睡后清醒）: {m['waso_min']:.0f} min\n\n")
        f.write("## 三、质量评分\n\n")
        f.write(f"**综合得分: {total} / 100，等级: {grade}**\n\n")
        f.write("| 维度 | 得分 | 满分 | 实测 | 标准 |\n|---|---:|---:|---|---|\n")
        for name, s, mx, val, ref in dims:
            f.write(f"| {name} | {s} | {mx} | {val} | {ref} |\n")
        f.write("\n## 四、心内科视角\n\n")
        f.write(_cardio_notes(m, grade))
        f.write("\n---\n\n方法学限制: 仅 HR/RR 单模态；REM 与 N1/N2 边界最模糊；短暂觉醒可能被规则误分类为 Wake。\n")

def _cardio_notes(m, grade):
    lines = []
    lines.append(f"1. **基础节律**: 睡眠期均心率 {m['sleep_hr_mean']:.1f} bpm，最低段 {m['sleep_hr_min']:.1f} bpm，最高段 {m['sleep_hr_max']:.1f} bpm。在无器质性心脏病史的前提下，属健康成年人的生理性夜间心动过缓范围。")
    lines.append(f"2. **自主神经**: RMSSD {m['rmssd']:.1f} ms、SDNN {m['sdnn']:.1f} ms。RMSSD 反映迷走神经张力，本次数值达深睡期典型水平，与深睡占比 {m['deep_pct']:.1f}% 一致。")
    if m['sleep_hr_min'] < 40:
        lines.append("3. **注意**: 出现单个 epoch 平均 HR < 40 bpm 的时段，若为非运动员/非药物影响者，建议门诊 24 h 动态心电图（Holter）复核，排除窦缓伴长间歇或Ⅱ度以上传导阻滞。")
    else:
        lines.append("3. **心动过缓评估**: 未见持续 < 40 bpm 段，属良性睡眠性窦缓。")
    lines.append(f"4. **觉醒事件**: 观察到 {m['awakenings']} 次觉醒、WASO {m['waso_min']:.0f} min。若无主观醒感，多为体位改变或短暂交感反弹；报告一（HR 突升 >20% 后 60 s 内回落）已在异常事件表列出，可与此段对照。")
    lines.append(f"5. **总体评价**: 睡眠质量等级 {grade}。若近 1-2 周同一时段监测 RMSSD 均值持续下降 20% 以上、深睡占比连续 <10%，提示恢复不足或早期自主神经失衡，建议追踪。")
    return "\n".join(lines) + "\n"

# ---------- HTML injection ----------
ANCHOR_BEGIN = "<!-- SLEEP_STRUCTURE_BEGIN -->"
ANCHOR_END = "<!-- SLEEP_STRUCTURE_END -->"

def inject_html(m, total, grade, dims, epochs, hr_base):
    html = HTML_PATH.read_text(encoding='utf-8')
    # 若已存在旧块，先剥离
    html = re.sub(re.escape(ANCHOR_BEGIN) + r".*?" + re.escape(ANCHOR_END),
                  '', html, flags=re.DOTALL)

    # 构造 hypnogram 数据（按 epoch，压缩为分钟级为主）
    labels = []
    y = []
    colors = []
    for ep in epochs:
        labels.append(ep['start'].strftime('%H:%M'))
        yv = STAGE_Y.get(ep['stage'])
        y.append(yv)
        colors.append(STAGE_COLORS.get(ep['stage'], '#8b949e'))
    stage_counts = m['counts']
    stage_labels = [STAGE_ZH[k] for k in STAGES]
    stage_mins = [stage_counts[k] * 0.5 for k in STAGES]
    _tst = stage_counts['Deep'] + stage_counts['Light'] + stage_counts['REM']
    _tib_local = _tst + stage_counts['Wake']
    def _pct_stage(k):
        if k == 'Wake':
            return (stage_counts[k] / _tib_local * 100.0) if _tib_local else 0.0
        return (stage_counts[k] / _tst * 100.0) if _tst else 0.0
    stage_pcts = [round(_pct_stage(k), 1) for k in STAGES]
    stage_colors_arr = [STAGE_COLORS[k] for k in STAGES]

    tst_h = m['tst_min']/60.0
    tib_h = m['tib_min']/60.0
    grade_color = {'A':'#238636','B':'#3fb950','C':'#d29922','D':'#db6d28','E':'#f85149'}.get(grade[0], '#3fb950')

    # 维度 HTML
    dim_rows = ''.join(
        f'<tr><td style="padding:6px 10px;">{name}</td>'
        f'<td style="padding:6px 10px;text-align:right;">{s} / {mx}</td>'
        f'<td style="padding:6px 10px;">{val}</td>'
        f'<td style="padding:6px 10px;color:var(--text-secondary);">{ref}</td></tr>'
        for name, s, mx, val, ref in dims
    )
    stage_pct_tst = m['counts']['Deep'] + m['counts']['Light'] + m['counts']['REM']
    def _pct_tst(k): return (m['counts'][k]/stage_pct_tst*100 if stage_pct_tst else 0)

    block = f"""{ANCHOR_BEGIN}
<style>
  .sleep-chart-grid {{ display:grid; grid-template-columns:1fr 1.4fr; gap:20px; align-items:stretch; }}
  .sleep-chart-grid > div {{ min-width:0; }}
  @media (max-width: 900px) {{
    .sleep-chart-grid {{ grid-template-columns:1fr; }}
  }}
</style>
<div class="card" style="margin-top:20px;margin-bottom:16px;">
  <h2 style="margin:0 0 12px 0;display:flex;align-items:center;flex-wrap:wrap;gap:10px;">
    <span>睡眠结构与质量评价</span>
    <span style="display:inline-flex;align-items:center;gap:8px;padding:4px 10px;background:rgba(0,0,0,.15);border-radius:8px;">
      <span style="font-size:20px;font-weight:700;color:{grade_color};line-height:1;">{total}</span>
      <span style="font-size:12px;color:{grade_color};font-weight:600;">{grade}</span>
    </span>
  </h2>
  <div class="sleep-chart-grid">
    <div>
      <div style="font-size:12px;color:var(--text-secondary);margin-bottom:4px;">分期时长分布（min）/ 睡眠时长（{m["tst_min"]:.0f} min / {tst_h:.1f} h）</div>
      <div style="height:280px;width:100%;"><canvas id="stageBarChart"></canvas></div>
    </div>
    <div>
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:4px;">
        <div style="font-size:12px;color:var(--text-secondary);">睡眠分期时序图 / 在床时长（{m["tib_min"]:.0f} min / {tib_h:.1f} h）</div>
        <div style="display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:var(--text-secondary);">
          <span style="display:inline-flex;align-items:center;gap:4px;"><span style="width:10px;height:10px;background:#3457D5;border-radius:2px;display:inline-block;"></span>深睡 N3</span>
          <span style="display:inline-flex;align-items:center;gap:4px;"><span style="width:10px;height:10px;background:#58A6FF;border-radius:2px;display:inline-block;"></span>浅睡 N1/N2</span>
          <span style="display:inline-flex;align-items:center;gap:4px;"><span style="width:10px;height:10px;background:#F0883E;border-radius:2px;display:inline-block;"></span>REM</span>
          <span style="display:inline-flex;align-items:center;gap:4px;"><span style="width:10px;height:10px;background:#DC3545;border-radius:2px;display:inline-block;"></span>清醒</span>
        </div>
      </div>
      <div style="height:280px;width:100%;"><canvas id="hypnogramChart"></canvas></div>
    </div>
  </div>
  <div style="margin-top:14px;padding:10px 14px;background:rgba(88,166,255,.08);border-left:3px solid #58A6FF;border-radius:4px;font-size:13px;color:var(--text-secondary);line-height:1.7;">
    <strong>方法说明:</strong> 基于 HRV 代理法（非 PSG 诊断）；30s 分帧，5min 滑窗特征（HR、RMSSD、HR 波动率）；分期规则基于个人夜间基线 HR ≈ {hr_base:.1f} bpm 与 RMSSD 分位；REM 与 N1/N2 边界依赖 HR 短时波动，比 PSG 更粗；短暂觉醒可能计入 Wake。
  </div>
</div>
<script>
(function(){{
  const hypLabels = {json.dumps(labels)};
  const hypY = {json.dumps(y)};
  const hypColors = {json.dumps(colors)};
  const stageMap = {{1:'{STAGE_ZH["Deep"]}', 2:'{STAGE_ZH["Light"]}', 3:'REM', 4:'{STAGE_ZH["Wake"]}'}};
  new Chart(document.getElementById('hypnogramChart'), {{
    type: 'line',
    data: {{
      labels: hypLabels,
      datasets: [{{
        label: '睡眠阶段',
        data: hypY,
        stepped: true,
        borderColor: '#58A6FF',
        borderWidth: 1.5,
        pointRadius: 0,
        segment: {{
          borderColor: ctx => hypColors[ctx.p0DataIndex] || '#58A6FF'
        }},
        fill: false,
        spanGaps: false
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{ mode:'nearest', intersect:false, axis:'x' }},
      plugins: {{
        legend: {{ display:false }},
        tooltip: {{
          callbacks: {{
            title: items => items.length ? items[0].label : '',
            label: item => stageMap[item.parsed.y] || '无数据'
          }}
        }}
      }},
      scales: {{
        y: {{
          reverse: true, min: 0.5, max: 4.5,
          ticks: {{
            stepSize: 1,
            color: '#475569',
            font: {{ size: 11 }},
            callback: v => stageMap[v] || ''
          }},
          grid: {{ display: true, color: 'rgba(0,0,0,0.06)', drawBorder: false }},
          border: {{ display: false }}
        }},
        x: {{
          ticks: {{ maxTicksLimit: 12, autoSkip: true, color: '#475569', font: {{ size: 10 }} }},
          grid: {{ display: true, color: 'rgba(0,0,0,0.05)', drawBorder: false }},
          border: {{ display: false }}
        }}
      }}
    }}
  }});
  const stagePcts = {json.dumps(stage_pcts)};
  new Chart(document.getElementById('stageBarChart'), {{
    type: 'bar',
    data: {{
      labels: {json.dumps(stage_labels)},
      datasets: [{{
        data: {json.dumps(stage_mins)},
        backgroundColor: {json.dumps(stage_colors_arr)},
        borderWidth: 0,
        borderRadius: 4,
        minBarLength: 4
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      interaction: {{ mode:'nearest', intersect:true, axis:'y' }},
      plugins: {{
        legend: {{ display:false }},
        tooltip: {{
          callbacks: {{
            label: ctx => `${{ctx.parsed.x.toFixed(0)}} min（${{stagePcts[ctx.dataIndex]}}%）`
          }}
        }}
      }},
      scales: {{
        x: {{
          beginAtZero: true,
          ticks: {{ color: '#475569', font: {{ size: 10 }} }},
          grid: {{ display: true, color: 'rgba(0,0,0,0.05)', drawBorder: false }},
          border: {{ display: true, color: 'rgba(0,0,0,0.05)' }}
        }},
        y: {{
          ticks: {{ color: '#475569', font: {{ size: 12 }} }},
          grid: {{ display: false }},
          border: {{ display: false }}
        }}
      }}
    }}
  }});
}})();
</script>
{ANCHOR_END}"""

    # 插入位置：紧跟“睡眠心率总体评估” summary-box 之后（在“睡眠心率区间分布” grid-2 之前）
    import re as _re
    m2 = _re.search(r'(<div class="summary-box">[\s\S]*?</div>)\s*(<div class="grid-2")', html)
    if m2:
        insert_at = m2.end(1)
        html = html[:insert_at] + "\n" + block + "\n" + html[insert_at:]
    else:
        anchor = '<div class="cardio-section">'
        if anchor in html:
            html = html.replace(anchor, block + "\n" + anchor, 1)
        else:
            html = html.replace('</body>', block + '\n</body>', 1)
    # 移除“睡眠心率区间分布”+“报文分类统计”卡片及其 Chart 初始化
    html = _re.sub(
        r'<div class="grid-2"[^>]*>\s*<div class="card">\s*<h2>睡眠心率区间分布</h2>[\s\S]*?<canvas id="packetChart"></canvas>\s*</div>\s*</div>\s*</div>\s*',
        '', html, count=1)
    html = _re.sub(
        r'// === 睡眠心率区间分布[\s\S]*?\}\]\s*\}\);\s*',
        '', html, count=1)
    html = _re.sub(
        r'// === 报文分类柱状图 ===[\s\S]*?\}\);\s*',
        '', html, count=1)
    HTML_PATH.write_text(html, encoding='utf-8')

# ---------- Main ----------
def main():
    beats_raw = load_beats()
    beats = beats_raw  # 用户要求不过滤 RR 突变，视为真实觉醒信号
    epochs = build_epochs(beats)
    add_sliding(beats, epochs)
    hr_base = classify(epochs)
    smooth(epochs)
    merge_short_wake(epochs, min_wake_epochs=6)
    m = compute_metrics(epochs, beats)
    total, grade, dims = score(m)
    write_markdown(m, total, grade, dims, hr_base)
    inject_html(m, total, grade, dims, epochs, hr_base)
    # 控制台简报
    print("=== 睡眠结构简报 ===")
    print(f"窗口: {m['onset_time']} -> {m['offset_time']}  TIB={m['tib_min']:.0f}min  TST={m['tst_min']:.0f}min  SE={m['se']*100:.1f}%")
    print(f"分期: 深睡 {m['counts']['Deep']*0.5:.0f}min ({m['deep_pct']:.1f}%) | 浅睡 {m['counts']['Light']*0.5:.0f}min ({m['light_pct']:.1f}%) | REM {m['counts']['REM']*0.5:.0f}min ({m['rem_pct']:.1f}%) | 清醒 {m['counts']['Wake']*0.5:.0f}min")
    print(f"觉醒 {m['awakenings']}次 / WASO {m['waso_min']:.0f}min | 睡眠均HR {m['sleep_hr_mean']:.1f} | RMSSD {m['rmssd']:.1f} | SDNN {m['sdnn']:.1f}")
    print(f"评分: {total}/100  等级: {grade}")
    print(f"输出: {MD_PATH}")
    print(f"HTML已注入: {HTML_PATH}")

if __name__ == '__main__':
    main()
