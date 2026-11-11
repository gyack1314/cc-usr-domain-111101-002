"""生成演示用虚构车队日志（跨午夜、复位、冲突模式、半边停用、零距离）。

资料写入与代码分离的目录（demo/data），生成器本身不位于资料目录内。
所有时间为 UTC，窗口 2026-11-10T23:58:00Z 至 2026-11-11T00:06:00Z，
运行日边界（UTC 午夜）落在窗口内部。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from datapack.storage import save_json  # noqa: E402

BASE = '2026-11-10T'
NEXT = '2026-11-11T'


def t(minute, second):
    if minute < 60:
        return f'{BASE}{23}:{minute:02d}:{second:02d}Z'
    return f'{NEXT}00:{minute - 60:02d}:{second:02d}Z'


modes, odometer, downtime, events = [], [], [], []


def add(kind, row):
    {'modes': modes, 'odometer': odometer, 'downtime': downtime,
     'events': events}[kind].append(row)


# ------------------------------------------------------------ V4 综合场景
# 模式：自动 -> 与人工重叠(冲突) -> 自动跨午夜 -> 无记录（不默认自动）。
add('modes', {'id': 'm40', 'vehicle': 'V4', 'start': t(58, 0), 'end': t(58, 45),
              'mode': 'AUTONOMOUS'})
add('modes', {'id': 'm41', 'vehicle': 'V4', 'start': t(58, 30), 'end': t(59, 0),
              'mode': 'MANUAL'})
add('modes', {'id': 'm42', 'vehicle': 'V4', 'start': t(59, 0), 'end': t(60, 40),
              'mode': 'AUTONOMOUS'})
# 停用区间：起点在窗口外、终点落在冲突段内（半边停用）。
add('downtime', {'id': 'd40', 'vehicle': 'V4', 'start': t(57, 50), 'end': t(58, 20)})
# 里程计：正常边 -> 跨午夜正常边 -> epoch 复位 -> 长缺口(15s+80s) ->
#         零距离静止边 -> 超速边。
odo4 = [
    ('o40', t(58, 0), 'e1', '0'),
    ('o41', t(58, 10), 'e1', '100'),
    ('o42', t(58, 20), 'e1', '200'),
    ('o43', t(58, 30), 'e1', '300'),
    ('o44', t(58, 40), 'e1', '400'),
    ('o45', t(58, 50), 'e1', '500'),
    ('o46', t(59, 0), 'e1', '600'),
    ('o47', t(59, 10), 'e1', '700'),       # 跨午夜
    ('o48', t(59, 20), 'e2', '0'),         # epoch 复位
    ('o49', t(59, 30), 'e2', '100'),
    ('o4m', t(60, 0), 'e2', '400'),        # 午夜点：跨午夜边在此切分，边界归右
    ('o4n', t(60, 10), 'e2', '500'),       # 午夜后可信边
    ('o4a', t(60, 25), 'e2', '650'),       # 间隔 15s > 上限 10s
    ('o4b', t(61, 45), 'e2', '800'),       # 间隔 80s 长缺口
    ('o4c', t(61, 55), 'e2', '800'),       # 零距离静止
    ('o4d', t(62, 5), 'e2', '1300'),       # 50m/s 超速
]
for oid, at, epoch, meter in odo4:
    add('odometer', {'id': oid, 'vehicle': 'V4', 'at': at,
                     'epoch': epoch, 'meter': meter})
# 事件：边界归右、冲突、停用、跨午夜、零距离、无模式、长缺口、窗口外各一。
for eid, at in [
    ('e40', t(58, 25)),    # 停用结束后，自动可信 -> 可计
    ('e41', t(58, 30)),    # 里程计/模式双边界 -> 归右侧冲突段 -> 不可计
    ('e42', t(58, 35)),    # 冲突段内部 -> 不可计
    ('e43', t(58, 15)),    # 停用内（半边停用）-> 不可计
    ('e44', t(59, 5)),     # 自动可信 -> 可计
    ('e45', t(61, 55)),    # 零距离静止边，但无模式且前边长缺口 -> 不可计
    ('e46', t(59, 15)),    # 复位边内部 -> 不可计（即便模式声称自动）
    ('e47', t(60, 45)),    # 长缺口 + 无模式 -> 不可计
    ('e48', t(57, 0)),     # 窗口外 -> 不可计
    ('e49', t(59, 20)),    # epoch 复位边界 -> 归右侧新域可信边 -> 可计
    ('e4a', t(60, 0)),     # 运行日午夜边界 -> 归右侧运行日与右边，自动可信 -> 可计
]:
    add('events', {'id': eid, 'vehicle': 'V4', 'at': at})

# ------------------------------------------------------------ V3 零距离事件
# PARKED 可信但零距离：事件可归属，比率必须拒绝给零风险率。
add('modes', {'id': 'm30', 'vehicle': 'V3', 'start': t(58, 0), 'end': t(59, 0),
              'mode': 'PARKED'})
for oid, at, meter in [
    ('o30', t(58, 0), '500'),
    ('o31', t(58, 10), '500'),
    ('o32', t(58, 20), '500'),
    ('o33', t(58, 30), '600'),      # epoch 复位后的新域
]:
    add('odometer', {'id': oid, 'vehicle': 'V3', 'at': at,
                     'epoch': 'p1' if oid != 'o33' else 'p2',
                     'meter': meter})
add('events', {'id': 'e30', 'vehicle': 'V3', 'at': t(58, 15)})
add('events', {'id': 'e31', 'vehicle': 'V3', 'at': t(58, 5)})

# ------------------------------------------------------------ V2 倒车与无模式
# 同 epoch 读数下降；后半程无模式记录（即便有里程也不默认自动）。
add('modes', {'id': 'm20', 'vehicle': 'V2', 'start': t(58, 0), 'end': t(59, 0),
              'mode': 'AUTONOMOUS'})
for oid, at, meter in [
    ('o20', t(58, 0), '0'),
    ('o21', t(58, 10), '100'),
    ('o22', t(58, 20), '80'),       # 倒车
    ('o23', t(58, 30), '180'),
    ('o24', t(58, 40), '280'),
    ('o25', t(58, 50), '380'),
    ('o26', t(59, 0), '480'),
    ('o27', t(59, 10), '580'),      # 无模式段
]:
    add('odometer', {'id': oid, 'vehicle': 'V2', 'at': at,
                     'epoch': 'k1', 'meter': meter})
add('events', {'id': 'e20', 'vehicle': 'V2', 'at': t(58, 25)})   # 倒车边 -> 不可计
add('events', {'id': 'e21', 'vehicle': 'V2', 'at': t(58, 35)})   # 自动可信 -> 可计
add('events', {'id': 'e22', 'vehicle': 'V2', 'at': t(59, 5)})    # 无模式 -> 不可计

bundle = {
    'name': '运行暴露量演示（虚构）',
    'files': [
        {'path': 'modes.json', 'key': 'id'},
        {'path': 'odometer.json', 'key': 'id'},
        {'path': 'downtime.json', 'key': 'id'},
        {'path': 'events.json', 'key': 'id'},
    ],
}
query = {
    'query_start': '2026-11-10T23:58:00Z',
    'query_end': '2026-11-11T00:06:00Z',
    'day_offset_minutes': 0,
    'max_gap_seconds': 10,
    'max_speed_mps': 30,
}


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    save_json(out / 'bundle.json', bundle)
    save_json(out / 'modes.json', modes)
    save_json(out / 'odometer.json', odometer)
    save_json(out / 'downtime.json', downtime)
    save_json(out / 'events.json', events)
    save_json(out / 'query.json', query)
    print('demo data written to', out)
    for name, rows in [('modes', modes), ('odometer', odometer),
                       ('downtime', downtime), ('events', events)]:
        print(f'  {name}.json: {len(rows)} rows')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else REPO / 'demo' / 'data')
