# 一同记账程序原型

本项目根据实验二的 UML 设计实现多人共享记账原型，使用 Python 3 标准库和 SQLite。

## 运行环境

- Python 3.11 或更高版本
- 无第三方运行依赖

## 快速验证

```powershell
python main.py demo
```

演示会创建两个账号、一个账本、两条收支记录、一条更正记录，完成审核、结算确认和快照生成。

## 常用命令

```powershell
python main.py --db data/yitong.db init
python main.py --db data/yitong.db register --account owner --name 韦依彤 --password Passw0rd!
python main.py --db data/yitong.db login --account owner --password Passw0rd!
python main.py --db data/yitong.db create-ledger --owner <用户ID> --name 旅行账本
python main.py --db data/yitong.db invite --admin <用户ID> --ledger <账本ID> --account member
python main.py --db data/yitong.db accept-invite --user <用户ID> --invitation <邀请ID>
python main.py --db data/yitong.db add-record --user <用户ID> --ledger <账本ID> --type EXPENSE --amount 120.00 --category 餐饮 --payer <成员ID> --shares <成员ID>=60,<成员ID>=60 --request-id req-001
python main.py --db data/yitong.db approve --user <审核人用户ID> --record <记录ID>
python main.py --db data/yitong.db statistics --user <用户ID> --ledger <账本ID>
python main.py --db data/yitong.db start-settlement --admin <用户ID> --ledger <账本ID>
python main.py --db data/yitong.db confirm-settlement --user <用户ID> --settlement <结算ID>
python main.py --db data/yitong.db logs --user <用户ID> --ledger <账本ID>
```

## 模块

- `auth_service.py`：登录、账号状态、平台管理员和日志。
- `ledger_service.py`：账本、邀请、成员、权限和管理员转让。
- `record_service.py`：收支、审核、更正、预算、查询和统计。
- `settlement_service.py`：结算、成员确认、快照和撤销。
- `reporting_service.py`：CSV 导出和脱敏。
- `cli.py`：命令行操作入口。

## 设计约定

- 金额在数据库中以整数分保存，避免浮点误差。
- PBKDF2 密码哈希；业务数据按 `ledger_id` 隔离。
- 记录状态为 PENDING、APPROVED、REJECTED、WITHDRAWN、VOIDED。
- 更正记录金额为正数，调增/调减由 `direction` 表示。
- 结构上的数据库约束和测试位于 `tests/`。