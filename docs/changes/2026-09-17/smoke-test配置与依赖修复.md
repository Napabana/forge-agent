# Smoke Test 配置与依赖修复

## 本轮目标
修复 `smoke_test.py` 在 Windows 环境下无法连接 API 的问题。

## 结论
根因不是 API 鉴权或网络：旧脚本用系统默认编码直接读取含中文内容的 `config/default.yaml`，会先触发 `UnicodeDecodeError`；同时没有复用正式配置加载器，也不会读取项目支持的 env 文件。当前解释器还缺少项目声明的 `openai` SDK。

## 实际修改
- `smoke_test.py` 改为复用 `config.schema.load_config`。
- 将解析后的 `AppConfig` 转换为现有 `create_backend_from_config` 所需的兼容字典。
- 在项目 `.venv` 安装 `openai>=1.30.0`。
- 修复 `ContextBudgetSpec` 深拷贝，使正式入口传入 `Task` 后可以正常写入 EventLog。

## 验证
- `\.venv\Scripts\python.exe -m py_compile smoke_test.py`：通过。
- 配置加载：通过，provider 为 `openai`，model 为 `deepseek-v4.1-flash`，API key 仅验证存在性。
- `OpenAICompatBackend` 实例化：通过，未发起真实 API 请求。
- `Task.to_dict()` 回归：通过，`budget_tokens` 序列化为普通 `int`。

## 边界
本轮未运行真实 LLM 冒烟请求，因此不宣称 API 调用成功；下一次运行应使用项目 `.venv`：

```powershell
.\.venv\Scripts\python.exe smoke_test.py
```
