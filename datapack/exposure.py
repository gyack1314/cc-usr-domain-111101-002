"""运行暴露量分段归算引擎。

只把能够证明属于指定运行模式、且落在可信里程计边覆盖内、未被停用区间
排除的时间与距离计入可信暴露量。所有区间按左闭右开处理；无记录不默认
自动，模式重叠且互相矛盾的时间标为 unknown。

设计要点：
- 先在模式与停用边界上切分时间轴，再用里程计边的端点（以及运行日边界）
  二次切分，得到不可再分的原子分段；
- 同一边切出的分段距离用十进制定点线性分摊，最后一个分段承担舍入余量，
  保证分段之和与边的窗口内增量严格相等（守恒）；
- epoch 之间绝不相减，不跨长缺口插值；停用优先于模式；
- 事件只在同模式、可信覆盖内计入对应分子，边界事件归右侧区间；
- 零距离分母拒绝给出零风险率（rate_status 而非数值 0）；
- 全部输出由输入指纹与口径指纹（配置+版本）共同标识，重复输入幂等。
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from .records import load_rows, index_rows
from .storage import fingerprint, read_json, save_json
from .domain import edge_issues, time_seconds

ENGINE_VERSION = 'exposure-2026.1'
# 距离十进制定点：米保留 6 位小数（微米），比率保留 9 位小数。
DISTANCE_QUANT = Decimal('0.000001')
RATE_QUANT = Decimal('0.000000001')
DAY_SECONDS = Decimal(86400)
EXCLUDED = 'EXCLUDED'
TRUSTED = 'TRUSTED'
UNKNOWN = 'UNKNOWN'

KNOWN_MODES = ('AUTONOMOUS', 'MANUAL', 'PARKED')
FILE_KINDS = ('modes', 'odometer', 'downtime', 'events')

# 停用优先于其他排除原因；里程计原因保留现有原语 edge_issues 的判定口径。
REASON_DOWNTIME = 'DOWNTIME'
REASON_NO_MODE = 'NO_MODE_RECORD'
REASON_CONFLICT = 'CONFLICTING_MODE'
REASON_NO_EDGE = 'NO_ODOMETER_COVERAGE'
REASON_EDGE_EPOCH = 'EDGE_EPOCH_CHANGE'
REASON_EDGE_GAP = 'EDGE_TIME_GAP'
REASON_EDGE_REVERSE = 'EDGE_COUNTER_REVERSE'
REASON_EDGE_SPEED = 'EDGE_SPEED_OUTLIER'
_EDGE_REASON = {
    'EPOCH_CHANGE': REASON_EDGE_EPOCH,
    'TIME_GAP': REASON_EDGE_GAP,
    'COUNTER_REVERSE': REASON_EDGE_REVERSE,
    'SPEED_OUTLIER': REASON_EDGE_SPEED,
}

# 事件不可计率原因。
EVENT_OUTSIDE_WINDOW = 'OUTSIDE_QUERY_WINDOW'
EVENT_NO_COVERAGE = 'NO_TRUSTED_COVERAGE'
EVENT_NO_MODE = 'NO_KNOWN_MODE'


def _q(value):
    return Decimal(value).quantize(DISTANCE_QUANT, rounding=ROUND_HALF_UP)


def _iso(seconds):
    """绝对秒（Decimal，UTC）转带偏移 ISO8601，支持微秒以下截尾。"""
    whole = int(seconds)
    micro = int(((seconds - whole) * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))
    if micro == 1_000_000:
        whole += 1
        micro = 0
    return datetime.fromtimestamp(whole, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S') + (
        f'.{micro:06d}Z' if micro else 'Z')


def load_inputs(root, bundle_name='bundle.json'):
    """按 bundle 声明读取四类记录，重复主键分类后只保留唯一记录。

    返回 {'rows': {kind: [记录...]}, 'duplicates': {kind: [id]},
    'conflicts': {kind: [id]}, 'bundle_digest': ...}。
    同主键不同内容（conflicts）拒绝加工，调用者须先修订资料；同主键同内容
    的重复记录去重，因此重复投递不会让暴露量翻倍。
    """
    from pathlib import Path
    root = Path(root)
    config = read_json(root / bundle_name)
    kind_by_path = {f'{kind}.json': kind for kind in FILE_KINDS}
    rows, duplicates, conflicts = {}, {}, {}
    for item in config['files']:
        kind = kind_by_path.get(item['path'])
        if kind is None:
            continue
        indexed = index_rows(load_rows(root / item['path']), item.get('key') or 'id')
        if indexed['conflicts']:
            raise ValueError(kind + ' 存在同主键不同内容的冲突记录：' + ','.join(indexed['conflicts']))
        rows[kind] = list(indexed['rows'].values())
        duplicates[kind] = indexed['duplicates']
        conflicts[kind] = indexed['conflicts']
    for kind in FILE_KINDS:
        rows.setdefault(kind, [])
        duplicates.setdefault(kind, [])
        conflicts.setdefault(kind, [])
    return {'rows': rows, 'duplicates': duplicates, 'conflicts': conflicts,
            'bundle_digest': fingerprint(config)}


def compute_exposure(rows, query_start, query_end, *,
                     max_gap_seconds=10, max_speed_mps=30, day_offset_minutes=0):
    """计算查询窗口内每车/运行日/模式的可信暴露量与事件分子。

    rows: {'modes': [...], 'odometer': [...], 'downtime': [...], 'events': [...]}，
    时间字段接受带偏移 ISO8601；query_start/query_end 同为带偏移 ISO8601。
    返回可直接 JSON 序列化的结构（Decimal 已转为定点字符串）。
    """
    start = time_seconds(query_start)
    end = time_seconds(query_end)
    if start >= end:
        raise ValueError('查询窗口须有正长度')
    max_gap = int(max_gap_seconds)
    max_speed = Decimal(str(max_speed_mps))
    if max_gap <= 0 or max_speed <= 0:
        raise ValueError('上限参数必须为正')
    offset = int(day_offset_minutes)
    config = {'max_gap_seconds': max_gap, 'max_speed_mps': str(max_speed),
              'day_offset_minutes': offset, 'engine_version': ENGINE_VERSION}

    vehicles = sorted({r['vehicle'] for kind in FILE_KINDS for r in rows.get(kind, ())})
    result_vehicles = []
    for vehicle in vehicles:
        result_vehicles.append(
            _vehicle_ledger(vehicle, rows, start, end, max_gap, max_speed, offset))

    totals = _aggregate(result_vehicles)
    payload = {
        'query_start': _iso(start),
        'query_end': _iso(end),
        'config': config,
        'vehicles': result_vehicles,
        'totals': totals,
        'input_fingerprint': fingerprint(rows),
    }
    payload['caliber_fingerprint'] = fingerprint({
        'config': config,
        'query_start': payload['query_start'],
        'query_end': payload['query_end'],
        'engine_version': ENGINE_VERSION,
    })
    payload['result_fingerprint'] = fingerprint({
        'input_fingerprint': payload['input_fingerprint'],
        'caliber_fingerprint': payload['caliber_fingerprint'],
        'totals': totals,
    })
    return payload


# ---------------------------------------------------------------- 单车主账

def _clip(lo, hi, start, end):
    """左闭右开区间与查询窗口求交，无交返回 None。"""
    lo = max(lo, start)
    hi = min(hi, end)
    return (lo, hi) if lo < hi else None


def _sweep_counts(points):
    """给 [(时刻, +1/-1)] 事件点，返回每个时刻之后的计数序列 [(t, count)]。"""
    current = 0
    out = []
    for t, delta in sorted(points, key=lambda x: (x[0], x[1])):
        current += delta
        out.append((t, current))
    return out


def _coverage_at(count_points, t):
    """t 处的活跃区间数（左闭右开：起点含、终点不含）。count_points 已排序。"""
    current = 0
    for cp, count in count_points:
        if cp > t:
            break
        current = count
    return current


def _day_of(t, offset):
    """按固定偏移把绝对秒归到运行日，标签为运行日本地日历日（ISO）。

    本地时间 = UTC + offset，故先把 UTC 时刻加上偏移再取日历日。
    """
    shifted = t + Decimal(offset * 60)
    day_index = (shifted / DAY_SECONDS).to_integral_value(rounding='ROUND_FLOOR')
    return datetime.fromtimestamp(int(day_index) * 86400, tz=timezone.utc).strftime('%Y-%m-%d')


def _day_cuts(start, end, offset):
    """窗口内的运行日边界：本地午夜映射回的 UTC 绝对秒。"""
    first_day = _day_of(start, offset)
    midnight_utc = datetime.fromisoformat(first_day + 'T00:00:00+00:00').replace(
        tzinfo=timezone.utc)
    epoch = midnight_utc - datetime(1970, 1, 1, tzinfo=timezone.utc)
    # 本地午夜（本地日历日 00:00）换算回 UTC：UTC = 本地 - offset。
    midnight = Decimal(epoch.days * 86400 + epoch.seconds) - Decimal(offset * 60)
    cuts = []
    t = midnight
    while t <= end:
        if start < t < end:
            cuts.append(t)
        t += DAY_SECONDS
    return cuts


def _build_edges(odo, start, end, max_gap, max_speed):
    """同 epoch 相邻点构造可信/不可信边，只保留与窗口相交者。"""
    points = sorted(odo, key=lambda r: (time_seconds(r['at']), r['id']))
    edges = []
    for a, b in zip(points, points[1:]):
        at_a, at_b = time_seconds(a['at']), time_seconds(b['at'])
        clipped = _clip(at_a, at_b, start, end)
        if clipped is None:
            continue
        issues = edge_issues(
            {'at': at_a, 'meter': a['meter'], 'epoch': a['epoch']},
            {'at': at_b, 'meter': b['meter'], 'epoch': b['epoch']},
            max_gap, max_speed)
        delta = Decimal(b['meter']) - Decimal(a['meter'])
        edges.append({
            'edge_id': a['id'] + '->' + b['id'],
            'from_id': a['id'], 'to_id': b['id'],
            'from_at': at_a, 'to_at': at_b,
            'window_lo': clipped[0], 'window_hi': clipped[1],
            'epoch': a['epoch'], 'trusted': not issues,
            'issues': issues,
            'reason': sorted(_EDGE_REASON[i] for i in issues),
            'edge_delta': delta,
        })
    return edges


def _edge_for(edges, t):
    """t 所属的边（左闭右开，边界点归右侧边）；无覆盖返回 None。"""
    for edge in edges:
        if edge['from_at'] <= t < edge['to_at']:
            return edge
    return None


def _vehicle_ledger(vehicle, rows, start, end, max_gap, max_speed, offset):
    modes = [r for r in rows['modes'] if r['vehicle'] == vehicle]
    odo = [r for r in rows['odometer'] if r['vehicle'] == vehicle]
    downs = [r for r in rows['downtime'] if r['vehicle'] == vehicle]
    events = [r for r in rows['events'] if r['vehicle'] == vehicle]

    # 1) 裁剪到窗口的模式/停用区间。
    mode_segs = []
    for r in modes:
        clipped = _clip(time_seconds(r['start']), time_seconds(r['end']), start, end)
        if clipped is None:
            continue
        mode_segs.append((clipped[0], clipped[1], r['mode']))
    down_segs, down_points = [], []
    for r in downs:
        clipped = _clip(time_seconds(r['start']), time_seconds(r['end']), start, end)
        if clipped is None:
            continue
        lo, hi = clipped
        down_segs.append((lo, hi))
        down_points += [(lo, 1), (hi, -1)]
    down_counts = _sweep_counts(down_points)

    # 2) 里程计边。
    edges = _build_edges(odo, start, end, max_gap, max_speed)

    # 3) 全部边界点：窗口、模式、停用、里程计端点、运行日午夜。
    cuts = {start, end}
    for lo, hi, _ in mode_segs:
        cuts.update((lo, hi))
    for lo, hi in down_segs:
        cuts.update((lo, hi))
    for edge in edges:
        if start < edge['from_at'] < end:
            cuts.add(edge['from_at'])
        if start < edge['to_at'] < end:
            cuts.add(edge['to_at'])
    cuts.update(_day_cuts(start, end, offset))
    timeline = sorted(cuts)

    # 4) 原子分段并分类。
    segments = []
    for lo, hi in zip(timeline, timeline[1:]):
        probe = lo  # 左闭右开，段内覆盖恒定
        active_modes = sorted({m for (a, b, m) in mode_segs if a <= probe < b})
        in_downtime = _coverage_at(down_counts, probe) > 0
        edge = _edge_for(edges, probe)
        reasons = []
        if in_downtime:
            reasons.append(REASON_DOWNTIME)
        if not active_modes:
            mode = None
            reasons.append(REASON_NO_MODE)
        elif len(set(active_modes)) > 1:
            mode = 'unknown'
            reasons.append(REASON_CONFLICT)
        else:
            mode = active_modes[0]
        if edge is None:
            reasons.append(REASON_NO_EDGE)
        elif not edge['trusted']:
            reasons.extend(edge['reason'])
        if in_downtime or mode is None or mode == 'unknown' or edge is None or not edge['trusted']:
            status = EXCLUDED
        else:
            status = TRUSTED
        segments.append({
            'lo': lo, 'hi': hi, 'mode': mode,
            'active_modes': active_modes, 'in_downtime': in_downtime,
            'edge_id': edge['edge_id'] if edge else None,
            'edge_trusted': bool(edge and edge['trusted']),
            'status': status, 'reasons': reasons,
            'day': _day_of(lo, offset),
        })

    # 5) 距离定点分摊：只对可信边按其窗口内增量分摊，末段承担舍入余量；
    #    不可信边的读数不进入任何距离累计，只在边台账中保留原始增量供审计。
    by_edge = {}
    for seg in segments:
        if seg['edge_id'] and seg['edge_trusted']:
            by_edge.setdefault(seg['edge_id'], []).append(seg)
    edge_ledger = []
    edge_by_id = {e['edge_id']: e for e in edges}
    for edge in edges:
        entry = {
            'edge_id': edge['edge_id'],
            'from_id': edge['from_id'], 'to_id': edge['to_id'],
            'from_at': _iso(edge['from_at']), 'to_at': _iso(edge['to_at']),
            'epoch': edge['epoch'], 'trusted': edge['trusted'],
            'issues': edge['issues'], 'reasons': edge['reason'],
            'edge_delta_meters': str(edge['edge_delta']),
            'window_lo': _iso(edge['window_lo']), 'window_hi': _iso(edge['window_hi']),
        }
        if not edge['trusted']:
            # 明确记录：不可信边不构成分母，窗口内增量被拒绝计入。
            entry['window_delta_meters'] = '0.000000'
            entry['fragment_meters_sum'] = '0.000000'
            entry['segment_count'] = 0
            edge_ledger.append(entry)
            continue
        segs = by_edge.get(edge['edge_id'], [])
        win_lo, win_hi = edge['window_lo'], edge['window_hi']
        span = win_hi - win_lo
        window_delta_q = _q(edge['edge_delta'] * (span / (edge['to_at'] - edge['from_at'])))
        running = Decimal(0)
        last = len(segs) - 1
        for idx, seg in enumerate(segs):
            if idx == last:
                meters = window_delta_q - running
            else:
                meters = _q(window_delta_q * ((seg['hi'] - seg['lo']) / span))
                running += meters
            seg['meters'] = meters
        entry.update({
            'window_delta_meters': str(window_delta_q),
            'segment_count': len(segs),
            'fragment_meters_sum': str(sum((s['meters'] for s in segs), Decimal(0))),
        })
        edge_ledger.append(entry)
    for seg in segments:
        seg.setdefault('meters', Decimal(0))

    # 6) 事件归属（边界时刻归右侧区间）。
    rateable, non_rateable, counters_raw = [], [], Counter()
    for ev in sorted(events, key=lambda r: (time_seconds(r['at']), r['id'])):
        at = time_seconds(ev['at'])
        if not (start <= at < end):
            non_rateable.append({'event_id': ev['id'], 'at': _iso(at),
                                 'reasons': [EVENT_OUTSIDE_WINDOW]})
            continue
        seg = next((s for s in segments if s['lo'] <= at < s['hi']), None)
        if seg is None or seg['status'] != TRUSTED:
            non_rateable.append({'event_id': ev['id'], 'at': _iso(at),
                                 'reasons': (seg['reasons'] if seg else [EVENT_NO_COVERAGE])})
            continue
        rateable.append({'event_id': ev['id'], 'at': _iso(at),
                         'mode': seg['mode'], 'day': seg['day'],
                         'segment_lo': _iso(seg['lo']), 'segment_hi': _iso(seg['hi'])})
        counters_raw[(seg['day'], seg['mode'])] += 1

    # 7) 汇总桶（车/日/模式），含 unknown 与排除原因分解。
    buckets = {}

    def bucket(day, mode):
        key = (day, mode)
        if key not in buckets:
            buckets[key] = {'day': day, 'mode': mode,
                            'trusted_seconds': Decimal(0), 'trusted_meters': Decimal(0),
                            'unknown_seconds': Decimal(0), 'excluded_seconds': Decimal(0),
                            'excluded_meters': Decimal(0),
                            'exclusion_reasons': Counter(), 'events': 0, 'event_ids': []}
        return buckets[key]

    reason_seconds = Counter()
    for seg in segments:
        dur = seg['hi'] - seg['lo']
        b = bucket(seg['day'], seg['mode'] if seg['mode'] is not None else 'unknown')
        if seg['status'] == TRUSTED:
            b['trusted_seconds'] += dur
            b['trusted_meters'] += seg['meters']
        else:
            b['excluded_seconds'] += dur
            b['excluded_meters'] += seg['meters']
            for reason in seg['reasons']:
                b['exclusion_reasons'][reason] += dur
                reason_seconds[reason] += dur
        if seg['mode'] is None or seg['mode'] == 'unknown':
            b['unknown_seconds'] += dur
    for (day, mode), count in counters_raw.items():
        b = bucket(day, mode)
        b['events'] += count
        b['event_ids'] = sorted(e['event_id'] for e in rateable
                                if e['day'] == day and e['mode'] == mode)

    summaries = []
    for (day, mode), b in sorted(buckets.items()):
        meters_q = _q(b['trusted_meters'])
        rate = None
        rate_status = 'OK'
        if meters_q == 0:
            rate_status = 'ZERO_DISTANCE_DENOMINATOR'
        elif b['events']:
            rate = (Decimal(b['events']) * Decimal(100000) / meters_q).quantize(
                RATE_QUANT, rounding=ROUND_HALF_UP)
        summaries.append({
            'day': day, 'mode': mode,
            'trusted_seconds': str(b['trusted_seconds']),
            'trusted_meters': str(meters_q),
            'unknown_seconds': str(b['unknown_seconds']),
            'excluded_seconds': str(b['excluded_seconds']),
            'excluded_meters': str(_q(b['excluded_meters'])),
            'exclusion_reasons': {k: str(v) for k, v in sorted(b['exclusion_reasons'].items())},
            'rateable_events': b['events'], 'event_ids': b['event_ids'],
            'rate_per_100km': (None if rate is None else str(rate)),
            'rate_status': rate_status,
        })

    seg_out = [{
        'lo': _iso(s['lo']), 'hi': _iso(s['hi']),
        'seconds': str(s['hi'] - s['lo']),
        'mode': s['mode'], 'active_modes': s['active_modes'],
        'in_downtime': s['in_downtime'],
        'edge_id': s['edge_id'], 'edge_trusted': s['edge_trusted'],
        'status': s['status'], 'reasons': s['reasons'],
        'meters': str(s['meters']), 'day': s['day'],
    } for s in segments]

    total_seconds = end - start
    trusted_seconds = sum((s['hi'] - s['lo'] for s in segments if s['status'] == TRUSTED), Decimal(0))
    unknown_seconds = sum((s['hi'] - s['lo'] for s in segments
                           if s['status'] == EXCLUDED and (s['mode'] is None or s['mode'] == 'unknown')),
                          Decimal(0))
    excluded_seconds = total_seconds - trusted_seconds
    trusted_meters = sum((s['meters'] for s in segments if s['status'] == TRUSTED), Decimal(0))
    excluded_meters = sum((s['meters'] for s in segments if s['status'] == EXCLUDED), Decimal(0))

    return {
        'vehicle': vehicle,
        'total_seconds': str(total_seconds),
        'trusted_seconds': str(trusted_seconds),
        'unknown_seconds': str(unknown_seconds),
        'excluded_seconds': str(excluded_seconds),
        'trusted_meters': str(_q(trusted_meters)),
        'excluded_meters': str(_q(excluded_meters)),
        'exclusion_reason_seconds': {k: str(v) for k, v in sorted(reason_seconds.items())},
        'mode_summaries': summaries,
        'segments': seg_out,
        'edge_ledger': sorted(edge_ledger, key=lambda e: e['from_at']),
        'rateable_events': rateable,
        'non_rateable_events': non_rateable,
    }


# ---------------------------------------------------------------- 车队汇总

def _aggregate(vehicle_ledgers):
    """车/日/模式三层汇总；距离与秒数由分段重新累加，比率在分母上重算。"""
    per_day, per_mode, fleet = {}, {}, Counter()
    fleet_meters = Decimal(0)
    for v in vehicle_ledgers:
        fleet['trusted_seconds'] += Decimal(v['trusted_seconds'])
        fleet['unknown_seconds'] += Decimal(v['unknown_seconds'])
        fleet['excluded_seconds'] += Decimal(v['excluded_seconds'])
        fleet_meters += Decimal(v['trusted_meters'])
        fleet['events'] += len(v['rateable_events'])
        for s in v['mode_summaries']:
            dkey = ('day', s['day'])
            mkey = ('mode', s['mode'])
            for key in (dkey, mkey):
                bucket = per_day if key[0] == 'day' else per_mode
                b = bucket.setdefault(key[1], {
                    'trusted_seconds': Decimal(0), 'trusted_meters': Decimal(0),
                    'unknown_seconds': Decimal(0), 'excluded_seconds': Decimal(0),
                    'excluded_meters': Decimal(0), 'events': 0})
                b['trusted_seconds'] += Decimal(s['trusted_seconds'])
                b['trusted_meters'] += Decimal(s['trusted_meters'])
                b['unknown_seconds'] += Decimal(s['unknown_seconds'])
                b['excluded_seconds'] += Decimal(s['excluded_seconds'])
                b['excluded_meters'] += Decimal(s['excluded_meters'])
                b['events'] += s['rateable_events']

    def finalize(bucket, label):
        out = []
        for key in sorted(bucket):
            b = bucket[key]
            meters = _q(b['trusted_meters'])
            rate = None
            status = 'OK'
            if meters == 0:
                status = 'ZERO_DISTANCE_DENOMINATOR'
            elif b['events']:
                rate = (Decimal(b['events']) * Decimal(100000) / meters).quantize(
                    RATE_QUANT, rounding=ROUND_HALF_UP)
            out.append({label: key,
                        'trusted_seconds': str(b['trusted_seconds']),
                        'trusted_meters': str(meters),
                        'unknown_seconds': str(b['unknown_seconds']),
                        'excluded_seconds': str(b['excluded_seconds']),
                        'excluded_meters': str(_q(b['excluded_meters'])),
                        'rateable_events': b['events'],
                        'rate_per_100km': (None if rate is None else str(rate)),
                        'rate_status': status})
        return out

    fleet_meters_q = _q(fleet_meters)
    fleet_rate = None
    fleet_status = 'OK'
    if fleet_meters_q == 0:
        fleet_status = 'ZERO_DISTANCE_DENOMINATOR'
    elif fleet['events']:
        fleet_rate = (Decimal(fleet['events']) * Decimal(100000) / fleet_meters_q).quantize(
            RATE_QUANT, rounding=ROUND_HALF_UP)
    return {
        'by_vehicle': [{
            'vehicle': v['vehicle'],
            'trusted_seconds': v['trusted_seconds'],
            'trusted_meters': v['trusted_meters'],
            'unknown_seconds': v['unknown_seconds'],
            'excluded_seconds': v['excluded_seconds'],
            'excluded_meters': v['excluded_meters'],
            'rateable_events': len(v['rateable_events']),
        } for v in vehicle_ledgers],
        'by_day': finalize(per_day, 'day'),
        'by_mode': finalize(per_mode, 'mode'),
        'fleet': {
            'trusted_seconds': str(fleet['trusted_seconds']),
            'trusted_meters': str(fleet_meters_q),
            'unknown_seconds': str(fleet['unknown_seconds']),
            'excluded_seconds': str(fleet['excluded_seconds']),
            'rateable_events': fleet['events'],
            'rate_per_100km': (None if fleet_rate is None else str(fleet_rate)),
            'rate_status': fleet_status,
        },
    }


def recompute(input_dir, query_start, query_end, *, max_gap_seconds=10,
              max_speed_mps=30, day_offset_minutes=0):
    """读取资料目录并重算；换上限或修订输入后直接再次调用即可。"""
    loaded = load_inputs(input_dir)
    payload = compute_exposure(
        loaded['rows'], query_start, query_end,
        max_gap_seconds=max_gap_seconds, max_speed_mps=max_speed_mps,
        day_offset_minutes=day_offset_minutes)
    payload['duplicates'] = loaded['duplicates']
    payload['conflicts'] = loaded['conflicts']
    payload['bundle_digest'] = loaded['bundle_digest']
    return payload


def write_result(path, payload):
    """定点结果落盘（canonical JSON，原子写入），便于指纹复核。"""
    save_json(path, payload)
