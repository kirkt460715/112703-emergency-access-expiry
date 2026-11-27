# 紧急查阅的临时身份授权与离线收回

所有时间是相对分钟，证明标识不是凭据。ack 记录接收端已同步的事实。 资料均为本地合成记录。

以 Python 3.11.2 运行，无第三方依赖。`records/` 中的 `identity`、`requests`、`checks` 对应独立来源。每份材料是 JSON 对象数组，`id` 在同一来源内唯一。报告和观察的时间以各原始记录给出的字段为准。

```sh
python3 -m unittest -v
python3 records_view.py
```

资料入口列出来源、记录标识及条数。通过 `--source` 指定来源，`--offset` 和 `--limit` 限定读取的位置与数量。`--records` 可指定同样结构的其他资料目录。

## 临时身份授权核验与提前收回

`urgent_access.py` 读取同样的 `identity`、`requests`、`checks` 三个来源，按原始记录给出的时间核验，不写回资料。身份记录只说明当班人最近完成的核验；批准必须另有独立审批人、审批时点和这一单的范围。

```sh
# 核验某单在时点 48 的资格；带上本次实际查阅的申请人、事项、机构与操作
python3 urgent_access.py --ticket ticket-a --at 48 --actor x --case E1 --region east --operation contact_view

# 只列本机构自己的记录（多机构互不可见）
python3 urgent_access.py --list-region east

# 登记提前结束与各端收到的撤回回执；输出仍需跟进的端点。
# 可加 --at 指定查询时点（默认取资料中已观察到的最晚时点），--out 另存处置结果。
python3 urgent_access.py --ticket ticket-a --end-at 51 --at 58 --receipt joint-desk@57 --out /tmp/disposal
```

核验要点：

* 申请人必须是该单当班人，且最近完成的身份核验覆盖审批时点与当班窗口；只看登录状态或身份记录不能当批准。
* 审批人须独立于申请人、在审批时仍可批准；未批准的记录（如 `ticket-b`）不具备任何临时资格。
* 资格绑定这一单、这一事项、这一机构与所列操作；不能转给接手其他事项的人，紧急访问也不沿用到其他区域。
* 有效窗口取计划结束、事态实际结束与当班结束中最早者；多机构只返回本机构记录。
* 提前结束按实际结束时间收回；`--receipt POINT@TIME`（或 `POINT@pending`）登记各端结果，确认时点早于结束或晚于查询都不算已确认。
* 接收端未确认不延长资格，到期后不能用于新的查阅。`events` 保留每条原始操作报告，并分别说明操作当时是否有依据（例如 48 分的操作有依据、52 分早于实际结束后的操作没有依据）。
* 处置结果默认只在输出中报告；`--out` 将结束与回执作为新记录写入指定目录，原始 `records/` 不被覆盖。
