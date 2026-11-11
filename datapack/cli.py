import argparse
import json
import sys
from pathlib import Path
from .records import inspect_bundle
from .storage import RevisionStore
from .exposure import recompute, write_result


def _emit(value):
    try:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    except BrokenPipeError:
        sys.stdout.close()


def main():
    parser=argparse.ArgumentParser(description='数据包内容检查与运行暴露量归算')
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('inspect');p.add_argument('bundle',type=Path)
    p=commands.add_parser('catalog');p.add_argument('database',type=Path);p.add_argument('stream')
    p=commands.add_parser('exposure',help='按查询窗口重算运行暴露量')
    p.add_argument('bundle',type=Path)
    p.add_argument('--start',required=True,help='查询起始时刻（带偏移 ISO8601，含）')
    p.add_argument('--end',required=True,help='查询结束时刻（带偏移 ISO8601，不含）')
    p.add_argument('--max-gap',type=int,default=10,dest='max_gap')
    p.add_argument('--max-speed',type=float,default=30.0,dest='max_speed')
    p.add_argument('--day-offset',type=int,default=0,dest='day_offset',help='运行日固定偏移分钟')
    p.add_argument('--output',type=Path,default=None,help='结果 JSON 落盘路径')
    args=parser.parse_args()
    try:
        if args.command=='inspect':
            value=inspect_bundle(args.bundle)
        elif args.command=='catalog':
            value=RevisionStore(args.database).latest(args.stream)
        else:
            value=recompute(args.bundle,args.start,args.end,
                            max_gap_seconds=args.max_gap,
                            max_speed_mps=args.max_speed,
                            day_offset_minutes=args.day_offset)
            if args.output is not None:
                write_result(args.output,value)
        _emit(value);return 0
    except (ValueError,KeyError,OSError):
        print('输入资料或目录无效，请检查路径及字段说明',file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
