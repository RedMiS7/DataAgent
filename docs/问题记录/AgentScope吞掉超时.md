# AgentScope 吞掉超时

日期：2026-11-07

状态：已修复，涉及分析模式的规划、补查、汇总和路由（`router.py`）

## 现象

分析评测中，有 2 题的日志写着“计划无法解析，改为直接分析”，模型原始回复是：

```
I notice the interruption. How can I help you?
```

这句话不是模型说的，是 AgentScope 的固定回复。

## 原因

用 `asyncio.wait_for(agent.reply(...), timeout)` 给 Agent 设超时。超时后，`wait_for` 会取消这个任务。

AgentScope 的 `Agent` 默认会捕获 `asyncio.CancelledError`，把它当作用户打断，然后正常返回一条回复，内容是 `ReActConfig.interruption_message`，默认值就是上面那句英文。`ReActConfig.interruption_raise_cancelled_error` 默认是 `False`，所以取消不会继续往外抛。

取消被吞掉后，`wait_for` 拿到的是一条正常回复，不会抛出 `TimeoutError`。我们的代码按正常回复去解析：

- 规划：解析不出 JSON，记成“计划无法解析”，实际是规划超过了 10 秒。
- 汇总：会把这句英文当成结论显示给用户。这次评测里没有出现。
- 路由：解析不出 JSON，记成“路由结果无法解析”，回退到查询模式。结果碰巧正确，但日志原因是错的。

## 排查方法

在 AgentScope 源码里搜这句话：

```bash
grep -rn "notice the interruption" .venv/lib/python3.12/site-packages/agentscope
```

找到 `agent/_config.py` 里的 `interruption_message` 默认值，再看 `agent/_agent.py` 中 `except asyncio.CancelledError` 的处理。

## 修复

给有超时的 Agent（规划、补查、汇总、路由）设置：

```python
react_config=ReActConfig(interruption_raise_cancelled_error=True)
```

取消会继续往外抛，`wait_for` 就能正常抛出 `TimeoutError`。

同时放宽了时限：规划和补查由 10 秒改为 20 秒，汇总由 30 秒改为 60 秒，路由由 8 秒改为 15 秒。

## 测试要注意

原来的超时测试是在调用 Agent 之前 `sleep`，取消发生在 AgentScope 外面，所以测不出这个问题。改为让测试模型本身变慢后，去掉修复，规划、补查、汇总、路由四个超时测试都会失败；路由会返回“判断结果无法解析”，而不是“判断超时”。
