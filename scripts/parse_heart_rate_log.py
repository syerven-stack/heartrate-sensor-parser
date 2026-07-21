#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XOSS心率设备BLE调试日志离线解析工具 V2.4 (纯Python标准库版)
- 兼容2/4/6/8字节全规格0x2A37报文，自动过滤畸形截断数据包
- 自动提取设备固件/SN/电量等设备参数
- 批量计算RR间期、瞬时真实心率
- 全量HRV时域指标运算
- 动态心率分段，自动计算运动负荷占比
- 输出3张Excel数据表(CSV) + 分段运动心律波动分析报告
"""

import argparse
import os
import re
import json
import csv
import math
from pathlib import Path
from collections import Counter, defaultdict
import zipfile
import io
import sys
import xml.etree.ElementTree as ET

# 运动模式识别（同目录模块，导入失败则优雅降级，不影响主解析）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from classify_exercise_mode import classify_exercise_mode
except Exception:
    classify_exercise_mode = None

# ==================== 全局常量 ====================
CH_TARGET = "ch=2A37"
BLE_RX_TAG = "[BLE.RX]"
RR_UNIT_SCALE = 1000.0 / 1024.0
HR_MIN = 30
HR_MAX = 220

# ==================== RR换算 ====================
def parse_rr(raw_low, raw_high):
    rr_raw = (raw_high << 8) + raw_low
    if rr_raw <= 0:
        return rr_raw, 0.0, float('nan')
    rr_ms = round(rr_raw * RR_UNIT_SCALE, 4)
    inst_hr = round(60.0 / (rr_raw / 1024.0), 2)
    return rr_raw, rr_ms, inst_hr

# ==================== 报文解析 ====================
def classify_packet_type(pkt_len):
    mapping = {
        2: "2字节(Flags+HR,无RR)",
        4: "4字节(1组RR)",
        6: "6字节(2组RR)",
        8: "8字节(3组RR)",
    }
    if pkt_len in mapping:
        return mapping[pkt_len]
    elif pkt_len > 8 and (pkt_len - 2) % 2 == 0:
        return f"{pkt_len}字节({(pkt_len-2)//2}组RR,扩展)"
    else:
        return f"{pkt_len}字节(非标准)"

def parse_packet(time_str, hex_list):
    packet_len = len(hex_list)
    flags = int(hex_list[0], 16)
    hr = int(hex_list[1], 16)
    rr_count = max(0, (packet_len - 2) // 2)

    base = {
        "time": time_str,
        "packet_len": packet_len,
        "packet_type": classify_packet_type(packet_len),
        "flags": flags,
        "report_hr": hr,
        "rr_count": rr_count,
        "hex_raw": " ".join(hex_list),
    }

    rr_list = []
    rr_ms_list = []
    inst_hr_list = []
    rr_data = hex_list[2:]
    for i in range(0, len(rr_data) - 1, 2):
        lo = int(rr_data[i], 16)
        hi = int(rr_data[i + 1], 16)
        rr_r, rr_m, ihr = parse_rr(lo, hi)
        rr_list.append(rr_r)
        rr_ms_list.append(rr_m)
        inst_hr_list.append(ihr)

    base["rr_raw_list"] = rr_list
    base["rr_ms_list"] = rr_ms_list
    base["inst_hr_list"] = inst_hr_list
    return base

# ==================== 统计工具函数 ====================
def mean(arr):
    return sum(arr) / len(arr) if arr else 0.0

def std(arr, ddof=1):
    if len(arr) < 2:
        return 0.0
    m = mean(arr)
    variance = sum((x - m) ** 2 for x in arr) / (len(arr) - ddof)
    return math.sqrt(variance)

def percentile(arr, p):
    if not arr:
        return 0.0
    sorted_arr = sorted(arr)
    k = (len(sorted_arr) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_arr[int(k)]
    return sorted_arr[f] * (c - k) + sorted_arr[c] * (k - f)

# ==================== 全量HRV指标 ====================
def calc_hrv(rr_ms_arr):
    rr = list(rr_ms_arr)
    n = len(rr)
    if n < 2:
        return {"error": "RR数据不足"}

    mean_rr = mean(rr)
    mean_hr = mean([60000.0 / r for r in rr if r > 0])
    sdnn = std(rr, ddof=1)

    diff_rr = [rr[i] - rr[i-1] for i in range(1, n)]
    abs_diff = [abs(d) for d in diff_rr]
    diff_sq = [d ** 2 for d in diff_rr]
    rmssd = math.sqrt(mean(diff_sq))
    sdsd = std(diff_rr, ddof=1)
    sdarr = std(abs_diff, ddof=1)
    pnn50 = sum(1 for d in abs_diff if d > 50) / len(abs_diff) * 100
    pnn20 = sum(1 for d in abs_diff if d > 20) / len(abs_diff) * 100
    cvrr = sdnn / mean_rr * 100 if mean_rr > 0 else 0.0

    min_rr = min(rr)
    max_rr = max(rr)
    min_hr = round(60000.0 / max_rr, 2) if max_rr > 0 else 0
    max_hr = round(60000.0 / min_rr, 2) if min_rr > 0 else 0

    # HRV三角指数近似
    try:
        bin_width = 1000.0 / 128.0
        hist = Counter()
        for r in rr:
            bin_idx = int(r / bin_width)
            hist[bin_idx] += 1
        max_count = max(hist.values()) if hist else 1
        tri_idx = n / max_count
    except Exception:
        tri_idx = 0.0

    # Tin (中位数RR)
    tin_rr = percentile(rr, 50)

    return {
        "总心跳数": n,
        "平均RR间期(ms)": round(mean_rr, 2),
        "平均瞬时心率(bpm)": round(mean_hr, 2),
        "最小RR间期(ms)": round(min_rr, 2),
        "最大RR间期(ms)": round(max_rr, 2),
        "最小心率(bpm)": min_hr,
        "最大心率(bpm)": max_hr,
        "SDNN(ms)": round(sdnn, 2),
        "RMSSD(ms)": round(rmssd, 2),
        "SDSD(ms)": round(sdsd, 2),
        "SDARR(ms)": round(sdarr, 2),
        "pNN50(%)": round(pnn50, 2),
        "pNN20(%)": round(pnn20, 2),
        "CVRR(%)": round(cvrr, 2),
        "HRV三角指数": round(tri_idx, 2),
        "Tin(ms)": round(tin_rr, 2),
        "RR极差(ms)": round(max_rr - min_rr, 2),
    }

# ==================== 运动负荷分段 ====================
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


# ACSM 心率区间比例：静息 <45%，热身 45-60%，有氧 60-75%，高强度 75-90%，极限 >90% HRmax
# HRmax 参考值选取（V2.4.4）：
#   1) 显式覆盖 hr_max_override（CLI --hr-max / _user_meta.json.hr_max）优先；
#   2) 否则 max(HR_MAX_DEFAULT=190, hr_p95)：默认按成年人常规上限 190 兜底，
#      只有当本次日志确实跑到过 ≥190 的心率时才用 P95 抬高。这样避免"没跑到极限
#      的样本 hr_p95 只有 165，导致 90% 缩放后的极限门槛仅 149、稳态跑就被算成
#      44% 极限占比"这类误读。
#   3) 极端兜底：所有心率序列都低于 100（纯静息数据）时仍走 190，不使用 P95。
HR_MAX_DEFAULT = 190.0

ZONE_RATIOS = [(0.00, 0.45), (0.45, 0.60), (0.60, 0.75), (0.75, 0.90), (0.90, 2.00)]
ZONE_NAMES = ("静息", "热身", "有氧", "高强度", "极限")


# 传统 key 名（HRmax=200 的历史遗留）；实际 bpm 阈值动态生成，通过每个 value 中的
# "阈值(bpm)" 字段暴露给下游报告，保证 iterator 语义稳定。
LEGACY_ZONE_KEYS = ["静息(<90)", "热身(90-120)", "有氧(120-150)", "高强度(150-180)", "极限(>180)"]


def split_exercise_segment(rr_rows, hr_max_override=None):
    all_hr = [r["inst_hr"] for r in rr_rows if 30 <= r.get("inst_hr", 0) <= 220]
    hr_p95 = _percentile(all_hr, 95) if all_hr else 0.0
    if hr_max_override and hr_max_override >= 100:
        # 显式覆盖：完全按调用方给的 HRmax 缩放
        hr_max_ref = float(hr_max_override)
        hr_max_source = "override"
    elif hr_p95 >= 100:
        # 默认策略：以 HR_MAX_DEFAULT(190) 兜底，若本次真跑到 ≥190 才用 P95 抬高
        hr_max_ref = max(HR_MAX_DEFAULT, hr_p95)
        hr_max_source = "p95" if hr_p95 >= HR_MAX_DEFAULT else "default_190"
    else:
        hr_max_ref = HR_MAX_DEFAULT
        hr_max_source = "default_190_no_signal"
    bins = [0.0] + [hi * hr_max_ref for _, hi in ZONE_RATIOS]

    zone_map = {}
    for row in rr_rows:
        hr = row["inst_hr"]
        idx = len(LEGACY_ZONE_KEYS) - 1
        for i in range(len(bins) - 1):
            if bins[i] <= hr < bins[i + 1]:
                idx = i
                break
        key = LEGACY_ZONE_KEYS[idx]
        row["hr_zone"] = key
        zone_map.setdefault(key, []).append(row)

    total = len(rr_rows)
    seg_info = {}
    for i, key in enumerate(LEGACY_ZONE_KEYS):
        rows = zone_map.get(key, [])
        count = len(rows)
        pct = count / total * 100 if total > 0 else 0
        avg_rr = mean([r["rr_ms"] for r in rows]) if rows else 0
        avg_hr = mean([r["inst_hr"] for r in rows]) if rows else 0
        lo = bins[i]
        hi = bins[i + 1] if i + 1 < len(bins) else 999.0
        seg_info[key] = {
            "心跳数": count,
            "占比(%)": round(pct, 2),
            "平均RR(ms)": round(avg_rr, 2) if rows else 0,
            "平均心率(bpm)": round(avg_hr, 2) if rows else 0,
            "阈值下界(bpm)": round(lo, 1),
            "阈值上界(bpm)": round(hi, 1) if hi < 900 else None,
        }
    seg_info["_meta"] = {"hr_max_ref": round(hr_max_ref, 1), "hr_p95": round(hr_p95, 1), "hr_max_source": hr_max_source}

    rest_pct = seg_info[LEGACY_ZONE_KEYS[0]]["占比(%)"]
    aerobic_pct = seg_info[LEGACY_ZONE_KEYS[1]]["占比(%)"] + seg_info[LEGACY_ZONE_KEYS[2]]["占比(%)"]
    high_pct = seg_info[LEGACY_ZONE_KEYS[3]]["占比(%)"] + seg_info[LEGACY_ZONE_KEYS[4]]["占比(%)"]
    summary = {
        "静息占比(%)": round(rest_pct, 2),
        "有氧运动占比(%)": round(aerobic_pct, 2),
        "高强度运动占比(%)": round(high_pct, 2),
    }
    return seg_info, summary

# ==================== 心律异常检测 ====================
def detect_arrhythmia(rr_rows):
    anomalies = []
    rr = [r["rr_ms"] for r in rr_rows]
    hr_vals = [r["inst_hr"] for r in rr_rows]

    for i in range(1, len(rr)):
        if rr[i - 1] > 0:
            change_pct = abs(rr[i] - rr[i - 1]) / rr[i - 1] * 100
            if change_pct > 20:
                anomalies.append({
                    "time": rr_rows[i]["time"],
                    "type": "RR间期突变",
                    "detail": f"RR变化{change_pct:.1f}% ({rr[i-1]:.0f}->{rr[i]:.0f}ms)",
                    "hr_before": round(60000/rr[i-1], 1) if rr[i-1] > 0 else 0,
                    "hr_after": round(60000/rr[i], 1) if rr[i] > 0 else 0,
                })

    for i in range(1, len(rr) - 1):
        if rr[i] < rr[i-1] * 0.8 and rr[i+1] > rr[i] * 1.2:
            anomalies.append({
                "time": rr_rows[i]["time"],
                "type": "疑似早搏",
                "detail": f"短RR({rr[i]:.0f}ms)后长RR({rr[i+1]:.0f}ms)",
                "hr_before": round(60000/rr[i-1], 1) if rr[i-1] > 0 else 0,
                "hr_after": round(60000/rr[i+1], 1) if rr[i+1] > 0 else 0,
            })

    out_of_range = sum(1 for h in hr_vals if h < HR_MIN or h > HR_MAX)
    return anomalies, out_of_range

# ==================== 设备信息提取 ====================
def extract_device_info(lines):
    device_info = {}
    for line in lines:
        ls = line.strip()
        m = re.search(r'firmware=([\d.]+)', ls)
        if m and 'firmware' not in device_info:
            device_info['firmware'] = m.group(1)
        m = re.search(r'fw=([\d.]+)', ls)
        if m and 'firmware' not in device_info:
            device_info['firmware'] = m.group(1)
        m = re.search(r'model=(\w+)', ls)
        if m and 'model' not in device_info:
            device_info['model'] = m.group(1)
        if 'ch=2A25' in ls and 'hex=' in ls:
            hex_str = ls.split('hex=')[-1].strip()
            try:
                sn = bytes(int(b, 16) for b in hex_str.split()).decode('ascii', errors='ignore')
                device_info['sn'] = sn
            except:
                pass
        m = re.search(r'更新电量:\s*(\d+)', ls)
        if m:
            device_info['battery'] = int(m.group(1))
        if 'XOSS_X2P_' in ls:
            m2 = re.search(r'XOSS_X2P_(\w+)', ls)
            if m2 and 'device_name' not in device_info:
                device_info['device_name'] = f"XOSS_X2P_{m2.group(1)}"
        m = re.search(r'remainStorage:\s*(\d+)', ls)
        if m and 'remain_storage' not in device_info:
            device_info['remain_storage'] = int(m.group(1))
    return device_info

# ==================== 生成XLSX (纯Python) ====================
def write_xlsx(filepath, sheets):
    """
    生成简单的.xlsx文件。
    sheets: {"Sheet名": [["列1","列2"], ["行1值1","行1值2"], ...]}
    """
    def col_letter(idx):
        result = ""
        while idx > 0:
            idx -= 1
            result = chr(65 + idx % 26) + result
            idx //= 26
        return result

    def make_sheet_xml(rows):
        xml_parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
        xml_parts.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
        xml_parts.append('<sheetData>')
        for row_idx, row in enumerate(rows, 1):
            xml_parts.append(f'<row r="{row_idx}">')
            for col_idx, val in enumerate(row, 1):
                cell_ref = f"{col_letter(col_idx)}{row_idx}"
                if isinstance(val, (int, float)):
                    xml_parts.append(f'<c r="{cell_ref}"><v>{val}</v></c>')
                else:
                    escaped = str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                    xml_parts.append(f'<c r="{cell_ref}" t="inlineStr"><is><t>{escaped}</t></is></c>')
            xml_parts.append('</row>')
        xml_parts.append('</sheetData></worksheet>')
        return "".join(xml_parts)

    # 构建xlsx (ZIP格式)
    with zipfile.ZipFile(filepath, 'w', zipfile.ZIP_DEFLATED) as zf:
        # [Content_Types].xml
        content_types = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        content_types += '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        content_types += '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        content_types += '<Default Extension="xml" ContentType="application/xml"/>'
        content_types += '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        for i in range(len(sheets)):
            content_types += f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        content_types += '</Types>'
        zf.writestr("[Content_Types].xml", content_types)

        # _rels/.rels
        rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        rels += '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        rels += '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        rels += '</Relationships>'
        zf.writestr("_rels/.rels", rels)

        # xl/workbook.xml
        wb = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        wb += '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        wb += '<sheets>'
        for i, (sheet_name, _) in enumerate(sheets.items()):
            wb += f'<sheet name="{sheet_name}" sheetId="{i+1}" r:id="rId{i+1}"/>'
        wb += '</sheets></workbook>'
        zf.writestr("xl/workbook.xml", wb)

        # xl/_rels/workbook.xml.rels
        wb_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        wb_rels += '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        for i in range(len(sheets)):
            wb_rels += f'<Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i+1}.xml"/>'
        wb_rels += '</Relationships>'
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)

        # 各sheet
        for i, (sheet_name, rows) in enumerate(sheets.items()):
            zf.writestr(f"xl/worksheets/sheet{i+1}.xml", make_sheet_xml(rows))

# ==================== 用户配置（CLI > _user_meta.json > 默认值） ====================
def _load_user_config(script_root, cli_flags):
    """读取 _user_meta.json，CLI 显式指定时覆盖。

    返回 dict: {xlsx, csv_raw, report_txt, hr_max}
    - xlsx/csv_raw/report_txt: bool，默认 True
    - hr_max: float 或 None（未配置）
    查找路径：脚本所在目录及其父级（skill 仓库根）。
    """
    config = {'xlsx': True, 'csv_raw': True, 'report_txt': True, 'hr_max': None}
    candidates = [script_root, script_root.parent]
    seen = set()
    for meta_dir in candidates:
        key = str(meta_dir)
        if key in seen:
            continue
        seen.add(key)
        meta_path = meta_dir / '_user_meta.json'
        if meta_path.is_file():
            try:
                _meta = json.loads(meta_path.read_text(encoding='utf-8'))
                if not isinstance(_meta, dict):
                    continue
                # outputs 块
                if 'outputs' in _meta:
                    outputs = _meta['outputs']
                    for k in ('xlsx', 'csv_raw', 'report_txt'):
                        if k in outputs and isinstance(outputs[k], bool):
                            config[k] = outputs[k]
                # hr_max
                if _meta.get('hr_max'):
                    config['hr_max'] = float(_meta['hr_max'])
            except Exception:
                pass
    # CLI 显式指定时覆盖（None 表示未传）
    for k in ('xlsx', 'csv_raw', 'report_txt'):
        if k in cli_flags and cli_flags[k] is not None:
            config[k] = bool(cli_flags[k])
    if cli_flags.get('hr_max') is not None:
        config['hr_max'] = float(cli_flags['hr_max'])
    return config


# ==================== 主处理函数 ====================
def process_single_log(log_path, out_dir, export_csv=True, export_json=True, user_config=None):
    # 加载用户配置（CLI > _user_meta.json > 默认值）
    if user_config is None:
        user_config = _load_user_config(Path(__file__).resolve().parent, {})
    hr_max_override = user_config['hr_max']
    output_config = user_config
    Path(out_dir).mkdir(exist_ok=True, parents=True)

    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    packet_rows = []
    rr_full_rows = []
    device_info = extract_device_info(lines)

    log_start_time = None
    log_end_time = None

    for line in lines:
        line = line.strip()
        if CH_TARGET in line and BLE_RX_TAG in line:
            try:
                parts = line.split(" ")
                time_str = parts[0] + " " + parts[1] if len(parts) > 1 else parts[0]
                if log_start_time is None:
                    log_start_time = time_str
                log_end_time = time_str

                hex_part = line.split("hex=")[-1].strip()
                hex_arr = hex_part.split()
                if len(hex_arr) < 2:
                    continue

                pkt = parse_packet(time_str, hex_arr)
                packet_rows.append(pkt)

                for idx, rrms in enumerate(pkt["rr_ms_list"]):
                    ihr = pkt["inst_hr_list"][idx]
                    if HR_MIN <= ihr <= HR_MAX and rrms > 0:
                        rr_full_rows.append({
                            "time": pkt["time"],
                            "packet_len": pkt["packet_len"],
                            "packet_type": pkt["packet_type"],
                            "report_hr": pkt["report_hr"],
                            "rr_raw": pkt["rr_raw_list"][idx],
                            "rr_ms": rrms,
                            "inst_hr": ihr,
                            "rr_index_in_packet": idx + 1,
                        })
            except Exception:
                continue

    if len(packet_rows) == 0:
        print("未检测到有效ch=2A37心率数据")
        return

    if len(rr_full_rows) == 0:
        print("未检测到有效RR间期数据")
        return

    # 报文分类统计
    pkt_type_counter = Counter()
    for p in packet_rows:
        pkt_type_counter[(p["packet_len"], p["packet_type"])] += 1
    pkt_stat = sorted(pkt_type_counter.items(), key=lambda x: x[0][0])
    total_pkts = len(packet_rows)

    # HRV计算
    rr_ms_arr = [r["rr_ms"] for r in rr_full_rows]
    hrv_res = calc_hrv(rr_ms_arr)

    # 运动负荷分段
    seg_detail, seg_summary = split_exercise_segment(rr_full_rows, hr_max_override=hr_max_override)

    # 心律异常检测
    anomalies, out_of_range_count = detect_arrhythmia(rr_full_rows)

    # 报文级别HR统计
    report_hrs = [p["report_hr"] for p in packet_rows]
    pkt_hr_stats = {
        "报文总数量": total_pkts,
        "有效心跳总数": len(rr_full_rows),
        "报文平均心率(bpm)": round(mean(report_hrs), 2),
        "报文心率标准差": round(std(report_hrs, ddof=1), 2),
        "报文最大心率(bpm)": max(report_hrs),
        "报文最小心率(bpm)": min(report_hrs),
        "RR间期异常数": out_of_range_count,
        "心律异常事件数": len(anomalies),
    }

    # ==================== 运动模式识别（启发式，心率/RR 时域） ====================
    exercise_mode_result = {}
    if classify_exercise_mode is not None:
        try:
            exercise_mode_result = classify_exercise_mode(
                rr_full_rows, hrv_res, seg_detail, pkt_hr_stats,
                {"start": log_start_time, "end": log_end_time}
            )
            # 轻量场景提示（与 generate_report.detect_scenario 口径一致）：
            # 静息占比>95% 且 平均心率<65 → 判定为睡眠/静息，运动模式不适用
            rest_pct = seg_detail.get("静息(<90)", {}).get("占比(%)", 0)
            avg_hr = hrv_res.get("平均瞬时心率(bpm)", 0)
            is_likely_sleep = (rest_pct > 95) and (avg_hr < 65)
            exercise_mode_result["scenario_hint"] = "sleep" if is_likely_sleep else "exercise"
            exercise_mode_result["applicable"] = not is_likely_sleep
        except Exception:
            exercise_mode_result = {}

    # ==================== 生成Excel ====================
    # Sheet1: 报文总表
    sheet1_header = ["time", "packet_len", "packet_type", "flags", "report_hr", "rr_count", "hex_raw"]
    sheet1_rows = [sheet1_header]
    for p in packet_rows:
        sheet1_rows.append([p["time"], p["packet_len"], p["packet_type"], p["flags"], p["report_hr"], p["rr_count"], p["hex_raw"]])

    # Sheet2: RR明细表
    sheet2_header = ["time", "packet_len", "packet_type", "report_hr", "rr_raw", "rr_ms", "inst_hr", "rr_index_in_packet", "hr_zone"]
    sheet2_rows = [sheet2_header]
    for r in rr_full_rows:
        sheet2_rows.append([r["time"], r["packet_len"], r["packet_type"], r["report_hr"], r["rr_raw"], r["rr_ms"], r["inst_hr"], r["rr_index_in_packet"], r.get("hr_zone", "")])

    # Sheet3: HRV汇总表
    sheet3_rows = [["类别", "指标", "数值"]]
    for k, v in device_info.items():
        sheet3_rows.append(["设备信息", k, v])
    for k, v in pkt_hr_stats.items():
        sheet3_rows.append(["报文统计", k, v])
    for (pkt_len, pkt_type), count in pkt_stat:
        pct = count / total_pkts * 100
        sheet3_rows.append(["报文分类统计", pkt_type, f"{count}条 ({pct:.2f}%)"])
    for k, v in hrv_res.items():
        sheet3_rows.append(["HRV时域指标", k, v])
    for k, v in seg_summary.items():
        sheet3_rows.append(["运动负荷分段", k, v])
    for zone, info in seg_detail.items():
        if zone.startswith("_"):
            continue
        sheet3_rows.append(["运动负荷明细", zone, f'心跳{info["心跳数"]}次, 占比{info["占比(%)"]}%, 平均心率{info["平均心率(bpm)"]}bpm'])

    if output_config['xlsx']:
        excel_path = Path(out_dir) / "心率解析汇总.xlsx"
        write_xlsx(str(excel_path), {
            "报文总表": sheet1_rows,
            "RR明细表": sheet2_rows,
            "HRV汇总表": sheet3_rows,
        })
        print(f"Excel已生成: {excel_path}")

    # ==================== 输出CSV ====================
    if export_csv:
        if output_config['csv_raw']:
            with open(Path(out_dir) / "报文数据.csv", "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerows(sheet1_rows)
        with open(Path(out_dir) / "心跳明细.csv", "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(sheet2_rows)
        print("CSV已生成")

    # ==================== 输出JSON ====================
    if export_json:
        out_json = {
            "device_info": device_info,
            "packet_stats": pkt_hr_stats,
            "packet_classification": [{"packet_len": pl, "packet_type": pt, "count": c, "pct": round(c/total_pkts*100, 2)} for (pl, pt), c in pkt_stat],
            "hrv_metrics": hrv_res,
            "exercise_segments": seg_detail,
            "exercise_summary": seg_summary,
            "anomalies": anomalies,
            "anomaly_count": len(anomalies),
            "log_time_range": {"start": log_start_time, "end": log_end_time},
            "exercise_mode": exercise_mode_result,
        }
        with open(Path(out_dir) / "分析结果.json", "w", encoding="utf-8") as jf:
            json.dump(out_json, jf, ensure_ascii=False, indent=2, default=str)
        print("JSON已生成")

    # ==================== 生成分析报告 ====================
    report = generate_report(
        device_info, pkt_hr_stats, pkt_stat, total_pkts, hrv_res,
        seg_detail, seg_summary, anomalies, log_start_time, log_end_time
    )
    if output_config['report_txt']:
        report_path = Path(out_dir) / "分段运动心律波动分析报告.txt"
        with open(report_path, "w", encoding="utf-8") as rf:
            rf.write(report)
        print(f"分析报告已生成: {report_path}")
    print(f"\n=== 解析完成! 输出目录: {out_dir} ===")
    return report

# ==================== 分析报告生成 ====================
def generate_report(device_info, pkt_stats, pkt_stat, total_pkts, hrv, seg_detail, seg_summary, anomalies, t_start, t_end):
    lines = []
    lines.append("=" * 70)
    lines.append("          分段运动心律波动分析报告")
    lines.append("=" * 70)
    lines.append("")
    lines.append("一、设备信息")
    lines.append("-" * 50)
    lines.append(f"  设备名称:   {device_info.get('device_name', '未知')}")
    lines.append(f"  设备型号:   {device_info.get('model', '未知')}")
    lines.append(f"  设备SN:     {device_info.get('sn', '未知')}")
    lines.append(f"  固件版本:   {device_info.get('firmware', '未知')}")
    lines.append(f"  电池电量:   {device_info.get('battery', '未知')}%")
    lines.append(f"  剩余存储:   {device_info.get('remain_storage', '未知')}")
    lines.append("")
    lines.append("二、数据概览")
    lines.append("-" * 50)
    lines.append(f"  记录时段:   {t_start} ~ {t_end}")
    for k, v in pkt_stats.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("三、报文分类统计")
    lines.append("-" * 50)
    lines.append(f"  {'报文类型':<35s} {'数量':>6s} {'占比':>8s}")
    lines.append(f"  {'-'*35} {'-'*6} {'-'*8}")
    for (pkt_len, pkt_type), count in pkt_stat:
        pct = count / total_pkts * 100
        lines.append(f"  {pkt_type:<35s} {count:>6d} {pct:>7.2f}%")
    lines.append(f"  {'合计':<35s} {total_pkts:>6d} {'100.00%':>8s}")
    lines.append("")
    lines.append("四、HRV心率变异性指标（全量时域）")
    lines.append("-" * 50)
    for k, v in hrv.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("五、运动负荷分段分析")
    lines.append("-" * 50)
    lines.append(f"  {'区间':<20s} {'心跳数':>6s} {'占比':>8s} {'平均RR(ms)':>12s} {'平均心率(bpm)':>14s}")
    lines.append(f"  {'-'*20} {'-'*6} {'-'*8} {'-'*12} {'-'*14}")
    for zone, info in seg_detail.items():
        if zone.startswith("_"):
            continue
        lines.append(f"  {zone:<20s} {info['心跳数']:>6d} {info['占比(%)']:>7.2f}% {info['平均RR(ms)']:>12.2f} {info['平均心率(bpm)']:>14.2f}")
    lines.append("")
    lines.append("  运动负荷汇总:")
    lines.append(f"    静息占比:     {seg_summary['静息占比(%)']}%")
    lines.append(f"    有氧运动占比: {seg_summary['有氧运动占比(%)']}%")
    lines.append(f"    高强度占比:   {seg_summary['高强度运动占比(%)']}%")
    lines.append("")

    lines.append("六、运动负荷评估")
    lines.append("-" * 50)
    if seg_summary['高强度运动占比(%)'] > 20:
        lines.append("  本次运动以高强度为主，属于剧烈运动负荷，")
        lines.append("  建议关注恢复期心率回落速度，避免过度训练。")
    elif seg_summary['有氧运动占比(%)'] > 30:
        lines.append("  本次运动以有氧运动为主，负荷适中，")
        lines.append("  有利于心肺功能提升，可长期坚持。")
    elif seg_summary['静息占比(%)'] > 60:
        lines.append("  本次记录以静息状态为主，运动负荷较低。")
        lines.append("  建议增加有氧运动以改善心肺功能。")
    else:
        lines.append("  运动负荷分布较为均衡，静息与运动交替。")
    lines.append("")

    lines.append("七、心律异常分析")
    lines.append("-" * 50)
    if len(anomalies) == 0:
        lines.append("  全程未检测到心律异常事件。")
        lines.append("  心率变化平滑，传感器佩戴稳定，信号质量良好。")
    else:
        lines.append(f"  共检测到 {len(anomalies)} 起心律异常事件:")
        anomaly_types = {}
        for a in anomalies:
            anomaly_types[a["type"]] = anomaly_types.get(a["type"], 0) + 1
        for atype, count in anomaly_types.items():
            lines.append(f"    - {atype}: {count}次")
        lines.append("")
        lines.append("  异常事件明细(前20条):")
        for i, a in enumerate(anomalies[:20]):
            lines.append(f"    [{i+1}] 时间:{a['time']} | {a['type']} | {a['detail']}")
    lines.append("")

    lines.append("八、心率变异性(HRV)评价")
    lines.append("-" * 50)
    sdnn = hrv.get("SDNN(ms)", 0)
    rmssd = hrv.get("RMSSD(ms)", 0)
    pnn50 = hrv.get("pNN50(%)", 0)
    if sdnn > 100:
        lines.append(f"  SDNN={sdnn}ms，心率变异性较高，副交感神经活性良好。")
    elif sdnn > 50:
        lines.append(f"  SDNN={sdnn}ms，心率变异性处于正常范围。")
    else:
        lines.append(f"  SDNN={sdnn}ms，心率变异性偏低，建议关注身体恢复状态。")
    if rmssd > 50:
        lines.append(f"  RMSSD={rmssd}ms，迷走神经调节能力较强。")
    elif rmssd > 20:
        lines.append(f"  RMSSD={rmssd}ms，迷走神经调节能力正常。")
    else:
        lines.append(f"  RMSSD={rmssd}ms，迷走神经调节能力偏弱，可能存在疲劳积累。")
    lines.append(f"  pNN50={pnn50}%，反映自主神经调节平衡。")
    lines.append("")

    lines.append("九、综合结论")
    lines.append("-" * 50)
    lines.append(f"  本次记录共采集 {pkt_stats.get('报文总数量', 0)} 条心率报文，")
    lines.append(f"  有效心跳 {pkt_stats.get('有效心跳总数', 0)} 次。")
    lines.append(f"  心率范围: {hrv.get('最小心率(bpm)', 0)}~{hrv.get('最大心率(bpm)', 0)} bpm，")
    lines.append(f"  平均心率: {hrv.get('平均瞬时心率(bpm)', 0)} bpm。")
    if len(anomalies) == 0:
        lines.append("  全程心律平稳，无明显早搏或信号丢失，")
        lines.append("  传感器佩戴稳定，数据质量可靠。")
    else:
        lines.append(f"  检测到 {len(anomalies)} 起心律波动事件，")
        lines.append("  建议结合运动场景进一步分析。")
    lines.append("")
    lines.append("=" * 70)
    lines.append("                    报告结束")
    lines.append("=" * 70)
    return "\n".join(lines)

# ==================== 批量解析 ====================
def batch_parse(folder, args):
    log_files = list(Path(folder).glob("*.txt"))
    for f in log_files:
        print(f"正在解析: {f.name}")
        out_sub = Path(args.out) / f.stem
        process_single_log(str(f), str(out_sub), args.csv, args.json, user_config=args.user_config)

# ==================== 入口 ====================
def main():
    parser = argparse.ArgumentParser(description="XOSS心率日志解析工具 V2.1")
    parser.add_argument("--log", type=str, help="单日志文件路径")
    parser.add_argument("--batch", type=str, help="批量日志文件夹路径")
    parser.add_argument("--out", default="./output", help="输出根目录")
    parser.add_argument("--csv", type=int, default=1, help="1导出CSV")
    parser.add_argument("--json", type=int, default=1, help="1导出JSON")
    parser.add_argument("--hr-max", type=float, default=None, dest="hr_max",
                        help="显式覆盖 HRmax（bpm），影响运动负荷区间划分；未指定时默认按 190 兜底，本次日志真跑到 ≥190 时才用 P95")
    parser.add_argument("--xlsx", type=int, default=None, help="0跳过 心率解析汇总.xlsx（默认从 _user_meta.json 读取，均未配置则产出）")
    parser.add_argument("--csv-raw", type=int, default=None, help="0跳过 报文数据.csv（默认从 _user_meta.json 读取，均未配置则产出）")
    parser.add_argument("--report-txt", type=int, default=None, help="0跳过 分段运动心律波动分析报告.txt（默认从 _user_meta.json 读取，均未配置则产出）")
    args = parser.parse_args()

    # 合并用户配置（CLI > _user_meta.json > 默认值）
    script_root = Path(__file__).resolve().parent
    cli_flags = {'xlsx': args.xlsx, 'csv_raw': getattr(args, 'csv_raw'), 'report_txt': getattr(args, 'report_txt'), 'hr_max': args.hr_max}
    user_config = _load_user_config(script_root, cli_flags)
    args.user_config = user_config

    if args.batch:
        batch_parse(args.batch, args)
    elif args.log:
        process_single_log(args.log, args.out, args.csv, args.json, user_config=args.user_config)
    else:
        print("必须指定 --log 单文件或 --batch 文件夹参数")

if __name__ == "__main__":
    main()
