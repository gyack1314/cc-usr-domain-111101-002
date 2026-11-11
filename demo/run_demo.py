"""运行暴露量分段归算演示（可直接运行，建议用 -I 隔离模式）。

用法：
    python -I demo/run_demo.py demo/data
    python -I demo/run_demo.py demo/data --state state/demo.sqlite3

内容：
1. 按 query.json 的窗口与上限重算暴露量，打印每车分段明细、边台账、
   车/日/模式汇总与事件归属；
2. 在程序内断言时间与距离的分段守恒（明细之和等于总量）；
3. 重复输入不翻倍、换上限产生新口径指纹、修订目录保留新旧两版结果。
资料目录与脚本目录分离，路径由参数传入。
"""
import argparse
import copy
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from datapack.exposure import recompute, load_inputs, compute_exposure  # noqa: E402
from datapack.storage import RevisionStore  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError('守恒校验失败：' + message)
    print('  [ok] ' + message)


def conservation_checks(payload, event_totals):
    print('\n== 守恒校验（分段明细 <-> 总量）==')
    q = Decimal('0.000001')
    fleet_trusted_meters = Decimal(0)
    for v in payload['vehicles']:
        segs, edges = v['segments'], v['edge_ledger']
        # 时间守恒：分段秒数之和恰为窗口长度。
        check(sum((Decimal(s['seconds']) for s in segs), Decimal(0)) == Decimal(v['total_seconds']),
              f"{v['vehicle']} 分段秒数之和 = 窗口 {v['total_seconds']}s")
        check(Decimal(v['trusted_seconds']) + Decimal(v['excluded_seconds'])
              == Decimal(v['total_seconds']),
              f"{v['vehicle']} 可信秒 + 排除秒 = 总秒")
        # 距离守恒：可信边窗口增量 = 其分段分摊之和 = 全部分段距离之和。
        edge_sum = Decimal(0)
        for e in edges:
            check(Decimal(e['fragment_meters_sum']) == Decimal(e['window_delta_meters']),
                  f"{v['vehicle']} 边 {e['edge_id']} 切分比例分摊之和 "
                  f"{e['fragment_meters_sum']} = 窗口增量 {e['window_delta_meters']}")
            edge_sum += Decimal(e['window_delta_meters'])
        seg_meter_sum = sum((Decimal(s['meters']) for s in segs), Decimal(0))
        check(seg_meter_sum == edge_sum,
              f"{v['vehicle']} 全部分段距离 {seg_meter_sum} = 可信边窗口增量之和 {edge_sum}")
        check(Decimal(v['trusted_meters']) <= edge_sum,
              f"{v['vehicle']} 可信距离 {v['trusted_meters']} 不超过可信边总量 {edge_sum}")
        # 模式汇总守恒。
        mode_sec = sum((Decimal(s['trusted_seconds']) for s in v['mode_summaries']), Decimal(0))
        mode_meter = sum((Decimal(s['trusted_meters']) for s in v['mode_summaries']), Decimal(0))
        check(mode_sec == Decimal(v['trusted_seconds']) and mode_meter == Decimal(v['trusted_meters']),
              f"{v['vehicle']} 模式汇总可信秒/距离与车主账一致")
        # 事件守恒：可计 + 不可计 = 全部事件（含窗口外）。
        check(len(v['rateable_events']) + len(v['non_rateable_events'])
              == event_totals[v['vehicle']],
              f"{v['vehicle']} 可计与不可计事件之和 = 事件总数")
        fleet_trusted_meters += Decimal(v['trusted_meters'])
    # 车队/运行日两层再聚合守恒。
    day_meters = sum((Decimal(d['trusted_meters']) for d in payload['totals']['by_day']), Decimal(0))
    mode_meters = sum((Decimal(d['trusted_meters']) for d in payload['totals']['by_mode']
                       if d['mode'] != 'unknown'), Decimal(0))
    check(day_meters == fleet_trusted_meters and mode_meters == fleet_trusted_meters,
          '运行日与模式两个维度的可信距离都等于车队总量 ' + str(fleet_trusted_meters))
    check(Decimal(payload['totals']['fleet']['trusted_meters']) == fleet_trusted_meters.quantize(q),
          'fleet 总量与分车累加一致')


def print_report(payload):
    print('窗口', payload['query_start'], '->', payload['query_end'])
    print('口径指纹', payload['caliber_fingerprint'][:16],
          ' 输入指纹', payload['input_fingerprint'][:16],
          ' 结果指纹', payload['result_fingerprint'][:16])
    for v in payload['vehicles']:
        print(f"\n-- 车辆 {v['vehicle']}：可信 {v['trusted_seconds']}s / {v['trusted_meters']}m，"
              f"未知 {v['unknown_seconds']}s，排除 {v['excluded_seconds']}s / {v['excluded_meters']}m")
        print('   分段明细（lo-hi | 日 | 状态 | 模式 | 秒 | 分摊米 | 来源边 | 原因）')
        for s in v['segments']:
            print(f"   {s['lo'][11:19]}-{s['hi'][11:19]} | {s['day']} | "
                  f"{s['status']:8s} | {str(s['mode']):10s} | {s['seconds']:>4s}s | "
                  f"{s['meters']:>12s} | {str(s['edge_id']):9s} | {s['reasons']}")
        print('   边台账：')
        for e in v['edge_ledger']:
            print('   {edge_id:9s} {epoch} 可信={trusted!s:5s} 边增量={edge_delta_meters:>5s} '
                  '窗口增量={window_delta_meters:>12s} 分摊和={fragment_meters_sum:>12s} '
                  '问题={issues}'.format(**e))
        print('   车/日/模式汇总：')
        for s in v['mode_summaries']:
            print(f"   {s['day']} {s['mode']:10s} 可信 {s['trusted_seconds']:>4s}s "
                  f"{s['trusted_meters']:>12s}m 未知={s['unknown_seconds']:>4s}s "
                  f"排除={s['excluded_seconds']:>4s}s 事件={s['rateable_events']} "
                  f"率/100km={s['rate_per_100km']} ({s['rate_status']})")
        print('   可计率事件：', [e['event_id'] for e in v['rateable_events']])
        print('   不可计率集合：', [(e['event_id'], e['reasons']) for e in v['non_rateable_events']])
    print('\n== 车队总量 ==')
    print(json.dumps(payload['totals'], ensure_ascii=False, indent=2))


def fingerprint_demo(data_dir, query):
    print('\n== 指纹、幂等与修订演示 ==')
    loaded = load_inputs(data_dir)
    first = compute_exposure(loaded['rows'], query['query_start'], query['query_end'],
                             max_gap_seconds=query['max_gap_seconds'],
                             max_speed_mps=query['max_speed_mps'],
                             day_offset_minutes=query['day_offset_minutes'])
    second = compute_exposure(copy.deepcopy(loaded['rows']), query['query_start'], query['query_end'],
                              max_gap_seconds=query['max_gap_seconds'],
                              max_speed_mps=query['max_speed_mps'],
                              day_offset_minutes=query['day_offset_minutes'])
    check(first['result_fingerprint'] == second['result_fingerprint'],
          '重复输入重算结果指纹不变（不翻倍）')
    # 重复投递整份记录：经读取层同主键同内容去重后，指纹不变、量不翻倍。
    from datapack.records import index_rows
    deduped = {k: list(index_rows(rows + rows, 'id')['rows'].values())
               for k, rows in loaded['rows'].items()}
    dup_payload = compute_exposure(deduped, query['query_start'], query['query_end'],
                                   max_gap_seconds=query['max_gap_seconds'],
                                   max_speed_mps=query['max_speed_mps'])
    check(dup_payload['input_fingerprint'] == first['input_fingerprint'],
          '同内容重复记录去重后输入指纹一致')
    reported = index_rows(loaded['rows']['events']
                          + loaded['rows']['events'], 'id')
    check(bool(reported['duplicates']) and not reported['conflicts'],
          '读取层把同内容重复主键报告为 duplicates 而非 conflicts')
    # 换上限：口径指纹与结果指纹都应改变，且长缺口边转为可信。
    widened = compute_exposure(loaded['rows'], query['query_start'], query['query_end'],
                               max_gap_seconds=15,
                               max_speed_mps=query['max_speed_mps'])
    check(widened['caliber_fingerprint'] != first['caliber_fingerprint'],
          'max_gap 10->15 产生新口径指纹')
    check(widened['result_fingerprint'] != first['result_fingerprint'],
          '新口径产生新结果指纹（旧版结果仍可由旧参数复算）')
    v4_old = next(v for v in first['vehicles'] if v['vehicle'] == V4)
    v4_new = next(v for v in widened['vehicles'] if v['vehicle'] == V4)
    check(Decimal(v4_new['trusted_meters']) > Decimal(v4_old['trusted_meters']),
          '放宽间隔上限后 V4 可信距离增加：%s -> %s'
          % (v4_old['trusted_meters'], v4_new['trusted_meters']))
    return first, widened


def revision_demo(state_path, first, widened):
    print('\n== 修订目录（新旧口径同流共存）==')
    store = RevisionStore(state_path)
    rev1 = store.put('exposure/demo', 0, first)
    rev2 = store.put('exposure/demo', rev1, widened)
    check(rev1 == 1 and rev2 == 2, '两次提交形成修订 1、2')
    check(store.put('exposure/demo', rev2, widened) == rev2, '相同内容幂等提交不新增修订')
    old = store.get('exposure/demo', rev1)['value']
    check(old['result_fingerprint'] == first['result_fingerprint'],
          '修订 1 保留旧口径结果与指纹')
    check(store.latest('exposure/demo')['revision'] == rev2, '最新修订为放宽上限后的版本')


V4 = 'V4'


def main():
    parser = argparse.ArgumentParser(description='运行暴露量分段归算演示')
    parser.add_argument('data_dir', type=Path)
    parser.add_argument('--state', type=Path, default=None)
    args = parser.parse_args()
    query = json.loads((args.data_dir / 'query.json').read_text(encoding='utf-8'))
    payload = recompute(args.data_dir, query['query_start'], query['query_end'],
                        max_gap_seconds=query['max_gap_seconds'],
                        max_speed_mps=query['max_speed_mps'],
                        day_offset_minutes=query['day_offset_minutes'])
    # 供守恒校验中的事件总数使用。
    loaded = load_inputs(args.data_dir)
    event_totals = {}
    for row in loaded['rows']['events']:
        event_totals[row['vehicle']] = event_totals.get(row['vehicle'], 0) + 1
    print_report(payload)
    conservation_checks(payload, event_totals)
    first, widened = fingerprint_demo(args.data_dir, query)
    state = args.state or Path(tempfile.mkdtemp()) / 'demo.sqlite3'
    revision_demo(state, first, widened)
    print('\n全部演示与守恒校验通过。状态库：', state)


if __name__ == '__main__':
    main()
