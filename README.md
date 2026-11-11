# 混编车队的运行暴露量分段归算数据包

混编运行的模式、里程和事件记录库，提供区间切分、距离单位工具与运行暴露量
归算引擎。数据文件位于samples，字段和边界见docs/domain.md，持久目录接口见
docs/storage.md，暴露量口径见docs/exposure.md。

## 本地运行

Python 3.11以上，仅依赖标准库，无需真实账号或外部数据服务。

```bash
python -m datapack.cli inspect samples
python -m unittest discover -s tests -v
python -m compileall -q datapack tests
# 虚构演示资料（跨午夜、复位、冲突模式、半边停用、零距离），含守恒断言：
python -I tools/make_demo_data.py demo/data
python -I demo/run_demo.py demo/data
# 按显式窗口与上限重算并落盘：
python -m datapack.cli exposure demo/data \
  --start 2026-11-10T23:58:00Z --end 2026-11-11T00:06:00Z \
  --output output/exposure.json
```

第一条命令检查已有资料，第二条运行读取、内容存储、领域原语与暴露量回归
检查，第三条构建Python字节码。演示脚本与资料目录分离，建议用 `-I` 隔离
模式运行，资料路径由参数传入。

## 容器

```bash
docker build -t local-datapack .
docker run --rm --network none local-datapack
docker run --rm --network none local-datapack python -m unittest discover -s tests -v
```

应用没有必须常驻的服务，入口和库可在同一容器离线运行。state和output是本地工作目录，已排除出Git。样本文件只读，修订目录使用独立SQLite文件，旧版本可继续查询。

## 目录

datapack/storage.py提供摘要、原子文件与修订目录；datapack/records.py提供文件读取及重复主键分类；datapack/domain.py提供当前领域原语；datapack/exposure.py在这些原语之上实现运行暴露量分段归算（可信/未知/排除、定点分摊、事件分子与指纹）；tests验证这些行为。数据加工结果不由资料检查命令自动生成。
