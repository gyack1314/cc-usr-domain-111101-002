"""运行暴露量引擎的回归检查（标准库 unittest，可直接运行）。

运行：
    python -m unittest tests.test_exposure -v
覆盖：左闭右开与边界归右、冲突 unknown、无记录不默认自动、停用优先、
epoch 复位不相减、长缺口/倒车/超速边拒绝、定点分摊守恒、跨午夜运行日、
零距离拒给零风险率、指纹幂等与换口径、读取层重复/冲突、修订目录联动。
"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from datapack.exposure import (
    compute_exposure, load_inputs, recompute, ENGINE_VERSION,
    TRUSTED, EXCLUDED, REASON_DOWNTIME, REASON_NO_MODE, REASON_CONFLICT,
    REASON_EDGE_EPOCH, REASON_EDGE_GAP, REASON_EDGE_REVERSE,
    REASON_EDGE_SPEED,
)
from datapack.records import index_rows
from datapack.storage import RevisionStore

ROOT = Path(__file__).resolve().parents[1]
Q = Decimal('0.000001')
T0 = '2026-11-10T23:58:00Z'


def at(second):
    """窗口起点后的相对秒 -> UTC ISO（仅用于本文件的小型夹具）。"""
    from datapack.domain import time_seconds
    from datapack.exposure import _iso
    return _iso(time_seconds(T0) + Decimal(second))


def rows(modes=(), odo=(), downtime=(), events=()):
    return {'modes': list(modes), 'odometer': list(odo),
            'downtime': list(downtime), 'events': list(events)}


def mode(mid, lo, hi, m, vehicle='V'):
    return {'id': mid, 'vehicle': vehicle, 'start': at(lo), 'end': at(hi), 'mode': m}


def odo(oid, sec, meter, epoch='e', vehicle='V'):
    return {'id': oid, 'vehicle': vehicle, 'at': at(sec), 'epoch': epoch, 'meter': str(meter)}


def event(eid, sec, vehicle='V'):
    return {'id': eid, 'vehicle': vehicle, 'at': at(sec)}


def downtime(did, lo, hi, vehicle='V'):
    return {'id': did, 'vehicle': vehicle, 'start': at(lo), 'end': at(hi)}


def only_vehicle(payload, vehicle='V'):
    matches = [v for v in payload['vehicles'] if v['vehicle'] == vehicle]
    assert len(matches) == 1
    return matches[0]


class ExposureTests(unittest.TestCase):
    def compute(self, data, end=100, **kw):
        return compute_exposure(data, T0, at(end), **kw)

    # -------------------------------------------------- 基础口径
    def test_trusted_chain_basic_totals(self):
        data = rows(
            modes=[mode('m1', 0, 30, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 20, 200), odo('o3', 30, 300)],
        )
        v = only_vehicle(self.compute(data, end=30))
        self.assertEqual(v['trusted_seconds'], '30')
        self.assertEqual(v['trusted_meters'], '300.000000')
        self.assertEqual(v['excluded_seconds'], '0')
        self.assertEqual(v['unknown_seconds'], '0')

    def test_no_mode_record_is_never_default_autonomous(self):
        data = rows(odo=[odo('o0', 0, 0), odo('o1', 10, 100)])
        v = only_vehicle(self.compute(data, end=10))
        self.assertEqual(v['trusted_seconds'], '0')
        self.assertEqual(v['excluded_seconds'], '10')
        self.assertEqual(v['unknown_seconds'], '10')
        self.assertIn(REASON_NO_MODE, v['segments'][0]['reasons'])
        self.assertEqual(v['segments'][0]['mode'], None)

    def test_conflicting_overlap_is_unknown_not_attributed(self):
        data = rows(
            modes=[mode('m1', 0, 20, 'AUTONOMOUS'), mode('m2', 10, 20, 'MANUAL')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 20, 200)],
        )
        v = only_vehicle(self.compute(data, end=20))
        statuses = {(s['lo'], s['hi']): s for s in v['segments']}
        overlap = next(s for s in v['segments'] if s['mode'] == 'unknown')
        self.assertEqual((overlap['lo'], overlap['hi']), (at(10), at(20)))
        self.assertEqual(overlap['status'], EXCLUDED)
        self.assertIn(REASON_CONFLICT, overlap['reasons'])
        # 仅前 10 秒可计入自动模式分母。
        auto = next(s for s in v['mode_summaries'] if s['mode'] == 'AUTONOMOUS')
        self.assertEqual(auto['trusted_seconds'], '10')
        self.assertEqual(auto['trusted_meters'], '100.000000')

    def test_same_mode_overlap_stays_known(self):
        data = rows(
            modes=[mode('m1', 0, 15, 'AUTONOMOUS'), mode('m2', 10, 20, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 20, 200)],
        )
        v = only_vehicle(self.compute(data, end=20))
        self.assertTrue(all(s['status'] == TRUSTED for s in v['segments']))
        self.assertEqual(v['trusted_meters'], '200.000000')

    def test_downtime_excludes_even_when_mode_claims_autonomous(self):
        data = rows(
            modes=[mode('m1', 0, 30, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 20, 200), odo('o3', 30, 300)],
            downtime=[downtime('d1', 10, 20)],
        )
        v = only_vehicle(self.compute(data, end=30))
        self.assertEqual(v['trusted_seconds'], '20')
        self.assertEqual(v['trusted_meters'], '200.000000')
        excluded = [s for s in v['segments'] if s['status'] == EXCLUDED]
        self.assertTrue(all(REASON_DOWNTIME in s['reasons'] for s in excluded))
        self.assertEqual(sum(int(Decimal(s['seconds'])) for s in excluded), 10)

    def test_half_open_clip_and_boundary_event_to_right(self):
        data = rows(
            # 模式从第 5 秒才开始；查询起点的事件落在无模式侧。
            modes=[mode('m1', 5, 20, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 20, 200)],
            events=[event('e0', 0), event('e1', 10), event('e2', 20)],
        )
        v = only_vehicle(self.compute(data, end=20))
        self.assertEqual([e['event_id'] for e in v['rateable_events']], ['e1'])
        # e2 落在查询窗口右端点（左闭右开）-> 窗口外。
        self.assertIn('e2', [e['event_id'] for e in v['non_rateable_events']])
        nr = {e['event_id']: e['reasons'] for e in v['non_rateable_events']}
        # e0 位于里程计点上，边界归右边（有可信覆盖），仅缺模式记录。
        self.assertEqual(nr['e0'], [REASON_NO_MODE])

    def test_epoch_change_never_subtracted(self):
        data = rows(
            modes=[mode('m1', 0, 30, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 500), odo('o1', 10, 600),
                 odo('o2', 20, 0, epoch='f'), odo('o3', 30, 100, epoch='f')],
        )
        v = only_vehicle(self.compute(data, end=30))
        self.assertEqual(v['trusted_seconds'], '20')
        self.assertEqual(v['trusted_meters'], '200.000000')
        reset_edge = next(e for e in v['edge_ledger'] if e['edge_id'] == 'o1->o2')
        self.assertFalse(reset_edge['trusted'])
        self.assertIn('EPOCH_CHANGE', reset_edge['issues'])
        self.assertEqual(reset_edge['window_delta_meters'], '0.000000')

    def test_long_gap_not_interpolated(self):
        data = rows(
            modes=[mode('m1', 0, 25, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 25, 250)],
        )
        default = only_vehicle(self.compute(data, end=25))
        self.assertEqual(default['trusted_meters'], '100.000000')
        gap = next(e for e in default['edge_ledger'] if e['edge_id'] == 'o1->o2')
        self.assertEqual(gap['issues'], ['TIME_GAP'])
        # 放宽上限后同一条边转为可信，且按真实时长线性分摊。
        widened = only_vehicle(self.compute(data, end=25, max_gap_seconds=20))
        self.assertEqual(widened['trusted_meters'], '250.000000')

    def test_reverse_and_speed_edges_rejected(self):
        data = rows(
            modes=[mode('m1', 0, 30, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 100), odo('o1', 10, 80),
                 odo('o2', 20, 180), odo('o3', 30, 680)],
        )
        v = only_vehicle(self.compute(data, end=30))
        reasons = {e['edge_id']: e['reasons'] for e in v['edge_ledger']}
        self.assertEqual(reasons['o0->o1'], [REASON_EDGE_REVERSE])
        self.assertEqual(reasons['o2->o3'], [REASON_EDGE_SPEED])
        # 只有 o1->o2（100m）进入距离分母；不可信读数不产生负里程。
        self.assertEqual(v['trusted_meters'], '100.000000')
        self.assertTrue(Decimal(v['excluded_meters']) >= 0)

    def test_proration_conservation_with_partial_edge(self):
        # 窗口从边内部开始（第 5 秒），边在模式/停用边界再切分。
        data = rows(
            modes=[mode('m1', 0, 15, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 20, 200)],
            downtime=[downtime('d1', 12, 15)],
        )
        payload = compute_exposure(data, at(5), at(20))
        v = only_vehicle(payload)
        for edge in v['edge_ledger']:
            self.assertEqual(edge['fragment_meters_sum'], edge['window_delta_meters'])
        seg_sum = sum((Decimal(s['meters']) for s in v['segments']), Decimal(0))
        edge_sum = sum((Decimal(e['window_delta_meters']) for e in v['edge_ledger']), Decimal(0))
        self.assertEqual(seg_sum, edge_sum)
        # 窗口截到边的一半：o0->o1 窗口增量为 50m。
        first = next(e for e in v['edge_ledger'] if e['edge_id'] == 'o0->o1')
        self.assertEqual(first['window_delta_meters'], '50.000000')

    def test_zero_distance_denominator_refuses_zero_rate(self):
        data = rows(
            modes=[mode('m1', 0, 20, 'PARKED')],
            odo=[odo('o0', 0, 500), odo('o1', 10, 500), odo('o2', 20, 500)],
            events=[event('e0', 5), event('e1', 15)],
        )
        v = only_vehicle(self.compute(data, end=20))
        self.assertEqual(v['trusted_seconds'], '20')
        self.assertEqual(v['trusted_meters'], '0.000000')
        self.assertEqual(len(v['rateable_events']), 2)
        summary = v['mode_summaries'][0]
        self.assertIsNone(summary['rate_per_100km'])
        self.assertEqual(summary['rate_status'], 'ZERO_DISTANCE_DENOMINATOR')
        self.assertEqual(payload_fleet_status(v), 'ZERO_DISTANCE_DENOMINATOR')

    def test_event_only_counts_in_same_mode_and_coverage(self):
        data = rows(
            modes=[mode('m1', 0, 10, 'AUTONOMOUS'), mode('m2', 10, 20, 'MANUAL')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 20, 200)],
            events=[event('e0', 5), event('e1', 15)],
        )
        v = only_vehicle(self.compute(data, end=20))
        ids = {e['event_id']: e['mode'] for e in v['rateable_events']}
        self.assertEqual(ids, {'e0': 'AUTONOMOUS', 'e1': 'MANUAL'})
        auto = next(s for s in v['mode_summaries'] if s['mode'] == 'AUTONOMOUS')
        manual = next(s for s in v['mode_summaries'] if s['mode'] == 'MANUAL')
        self.assertEqual(auto['rateable_events'], 1)
        self.assertEqual(manual['rateable_events'], 1)
        # 1 事件 / 100m -> 1000 次每 100km。
        self.assertEqual(auto['rate_per_100km'], '1000.000000000')

    def test_cross_midnight_day_split(self):
        data = rows(
            modes=[mode('m1', 0, 180, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 60, 600),
                 odo('o2', 120, 1200), odo('o3', 180, 1800)],
            events=[event('e0', 120)],
        )
        payload = compute_exposure(data, '2026-11-10T23:58:00Z', '2026-11-11T00:01:00Z',
                                   max_gap_seconds=60)
        v = only_vehicle(payload)
        days = {s['day'] for s in v['segments']}
        self.assertEqual(days, {'2026-11-10', '2026-11-11'})
        by_day = {d['day']: d for d in payload['totals']['by_day']}
        self.assertEqual(by_day['2026-11-10']['trusted_seconds'], '120')
        self.assertEqual(by_day['2026-11-11']['trusted_seconds'], '60')
        rated = next(e for e in v['rateable_events'] if e['event_id'] == 'e0')
        self.assertEqual(rated['day'], '2026-11-11')

    def test_day_offset_groups_by_local_midnight(self):
        data = rows(
            modes=[mode('m1', 0, 180, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 60, 600),
                 odo('o2', 120, 1200), odo('o3', 180, 1800)],
        )
        # +08:00 下 UTC 23:58-00:01 对应本地 07:58-08:01，同一运行日，
        # 标签为运行日本地日历日 2026-11-11。
        shifted = compute_exposure(
            data, '2026-11-10T23:58:00Z', '2026-11-11T00:01:00Z',
            max_gap_seconds=60, day_offset_minutes=480)
        self.assertEqual([d['day'] for d in shifted['totals']['by_day']], ['2026-11-11'])
        self.assertEqual(shifted['totals']['by_day'][0]['trusted_seconds'], '180')
        # 同一资料在零偏移下被 UTC 午夜切成两个运行日。
        zero = compute_exposure(
            data, '2026-11-10T23:58:00Z', '2026-11-11T00:01:00Z',
            max_gap_seconds=60, day_offset_minutes=0)
        self.assertEqual([d['day'] for d in zero['totals']['by_day']],
                         ['2026-11-10', '2026-11-11'])
        # 负偏移（-05:00）：UTC 23:58-00:01 是本地 18:58-19:01，
        # 不跨本地午夜，全部落在本地 10 日。
        west = compute_exposure(
            data, '2026-11-10T23:58:00Z', '2026-11-11T00:01:00Z',
            max_gap_seconds=60, day_offset_minutes=-300)
        self.assertEqual([d['day'] for d in west['totals']['by_day']],
                         ['2026-11-10'])

    def test_fixed_point_conservation_and_aggregation(self):
        data = rows(
            modes=[mode('m1', 0, 30, 'AUTONOMOUS')],
            # 300m/30s 边在非整十秒边界切分，制造无限小数分摊。
            odo=[odo('o0', 0, 0), odo('o1', 30, 300)],
            downtime=[downtime('d1', 7, 13)],
        )
        payload = compute_exposure(data, T0, at(30), max_gap_seconds=30)
        v = only_vehicle(payload)
        edge = v['edge_ledger'][0]
        self.assertEqual(edge['fragment_meters_sum'], edge['window_delta_meters'])
        # 全部为定点字符串，最多 6 位小数。
        for s in v['segments']:
            self.assertRegex(s['meters'], r'^\d+\.\d{6}$')
        day_meters = sum((Decimal(d['trusted_meters']) for d in payload['totals']['by_day']), Decimal(0))
        self.assertEqual(day_meters, Decimal(v['trusted_meters']))

    def test_fingerprints_stable_and_caliber_changes(self):
        data = rows(
            modes=[mode('m1', 0, 25, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 25, 250)],
        )
        import copy
        p1 = compute_exposure(copy.deepcopy(data), T0, at(25))
        p2 = compute_exposure(copy.deepcopy(data), T0, at(25))
        self.assertEqual(p1['result_fingerprint'], p2['result_fingerprint'])
        self.assertEqual(p1['config']['engine_version'], ENGINE_VERSION)
        widened = compute_exposure(data, T0, at(25), max_gap_seconds=25)
        self.assertNotEqual(widened['caliber_fingerprint'], p1['caliber_fingerprint'])
        self.assertNotEqual(widened['result_fingerprint'], p1['result_fingerprint'])
        self.assertEqual(widened['input_fingerprint'], p1['input_fingerprint'])

    def test_duplicate_rows_dedup_but_conflicts_rejected(self):
        clean = rows(
            modes=[mode('m1', 0, 10, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100)],
        )
        dup = {k: v + v for k, v in clean.items()}
        indexed = index_rows(dup['modes'], 'id')
        self.assertEqual(indexed['duplicates'], ['m1'])
        deduped = {k: list(index_rows(v, 'id')['rows'].values()) for k, v in dup.items()}
        p1 = compute_exposure(clean, T0, at(10))
        p2 = compute_exposure(deduped, T0, at(10))
        self.assertEqual(p1['result_fingerprint'], p2['result_fingerprint'])
        # 同主键不同内容必须由读取层拒绝。
        with self.assertRaises(ValueError):
            load_from_temp({'bundle.json': {'name': 'x', 'files': [
                {'path': 'modes.json', 'key': 'id'},
                {'path': 'odometer.json', 'key': 'id'},
                {'path': 'downtime.json', 'key': 'id'},
                {'path': 'events.json', 'key': 'id'}]},
                'modes.json': [mode('m1', 0, 10, 'AUTONOMOUS'),
                               dict(mode('m1', 0, 10, 'MANUAL'))],
                'odometer.json': [odo('o0', 0, 0), odo('o1', 10, 100)],
                'downtime.json': [], 'events.json': []})

    def test_demo_bundle_conservation_and_scenarios(self):
        payload = recompute(ROOT / 'demo' / 'data',
                            '2026-11-10T23:58:00Z', '2026-11-11T00:06:00Z')
        for v in payload['vehicles']:
            self.assertEqual(
                sum((Decimal(s['seconds']) for s in v['segments']), Decimal(0)),
                Decimal(v['total_seconds']))
            self.assertEqual(
                Decimal(v['trusted_seconds']) + Decimal(v['excluded_seconds']),
                Decimal(v['total_seconds']))
            edge_sum = sum((Decimal(e['window_delta_meters']) for e in v['edge_ledger']), Decimal(0))
            seg_sum = sum((Decimal(s['meters']) for s in v['segments']), Decimal(0))
            self.assertEqual(edge_sum, seg_sum)
            for e in v['edge_ledger']:
                self.assertEqual(e['fragment_meters_sum'], e['window_delta_meters'])
        v4 = only_vehicle(payload, 'V4')
        # 半边停用、冲突 unknown、复位、长缺口、超速、零距离原因都应出现。
        reasons = {r for s in v4['segments'] for r in s['reasons']}
        for expected in (REASON_DOWNTIME, REASON_CONFLICT, REASON_EDGE_EPOCH,
                         REASON_EDGE_GAP, REASON_EDGE_SPEED, REASON_NO_MODE):
            self.assertIn(expected, reasons)
        # 零距离静止边本身可信但读数为 0。
        zero_edge = next(e for e in v4['edge_ledger'] if e['edge_id'] == 'o4b->o4c')
        self.assertTrue(zero_edge['trusted'])
        self.assertEqual(zero_edge['window_delta_meters'], '0.000000')
        # 午夜边界事件归右侧运行日且可计。
        e4a = next(e for e in v4['rateable_events'] if e['event_id'] == 'e4a')
        self.assertEqual(e4a['day'], '2026-11-11')
        # V3 零距离分母拒绝零风险率。
        v3 = only_vehicle(payload, 'V3')
        self.assertTrue(all(s['rate_status'] == 'ZERO_DISTANCE_DENOMINATOR'
                            for s in v3['mode_summaries']
                            if s['mode'] == 'PARKED'))

    def test_revision_store_roundtrip_keeps_calibers(self):
        data = rows(
            modes=[mode('m1', 0, 25, 'AUTONOMOUS')],
            odo=[odo('o0', 0, 0), odo('o1', 10, 100), odo('o2', 25, 250)],
        )
        old = compute_exposure(data, T0, at(25))
        new = compute_exposure(data, T0, at(25), max_gap_seconds=25)
        with tempfile.TemporaryDirectory() as d:
            store = RevisionStore(Path(d) / 'r.sqlite3')
            r1 = store.put('exposure/test', 0, old)
            r2 = store.put('exposure/test', r1, new)
            self.assertEqual(store.get('exposure/test', r1)['value']['result_fingerprint'],
                             old['result_fingerprint'])
            self.assertEqual(store.latest('exposure/test')['revision'], r2)
            self.assertEqual(store.put('exposure/test', r2, new), r2)


def payload_fleet_status(vehicle_ledger):
    # 车级零距离状态在 mode_summaries 上体现；辅助断言。
    return vehicle_ledger['mode_summaries'][0]['rate_status']


def load_from_temp(files):
    from datapack.storage import save_json
    with tempfile.TemporaryDirectory() as d:
        for name, value in files.items():
            save_json(Path(d) / name, value)
        return load_inputs(d)


if __name__ == '__main__':
    unittest.main()
