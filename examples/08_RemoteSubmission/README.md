# 08 — 远程提交（单机 / 多机）

把本地的 INP 批量提交到一台或多台**远程 Windows 机器**执行，只回传体积小的
`.sta` / `.msg` / `.dat` 和提取结果，GB 级的 `.odb` 留在产出它的机器上。

远程是**严格 opt-in** 的：不传 `hosts=`，`BatchAbaqusProcessor` 的行为与以前完全一致。

```python
from ABQflow import BatchAbaqusProcessor, HostSpec

processor = BatchAbaqusProcessor(
    batch_data=specs,
    base_output_dir='output',
    cpus_per_job=2,
    hosts=[...],          # 唯一让它变成远程的一行
)
outcomes = processor.run_batch(num_parallel_jobs=4)
```

## 两个不要混淆的旋钮

| 参数 | 含义 | 什么时候设 |
|---|---|---|
| `max_concurrent` | 这台机器**同时**跑几个作业 | 想要"A 机 2 个、B 机 1 个"时 |
| `weight` | 这台机器分到**多大比例**的作业 | 机器的实际速度和核数不成比例时 |

不设 `weight` 时，默认按 `max_concurrent`（或从核数/token 推导出的容量）分配。

**为什么要分开**：开发时用的两台机器里，核数只有一半的那台跑同一个作业**快 60%**。
只按核数排序会把大部分作业送给慢的那台。`weight` 就是用来纠正这一点的。

```python
HostSpec(
    name='node02',
    hostname='NODE02', username='abaquser',
    password=os.environ['ABQFLOW_NODE02_PASSWORD'],
    abaqus_exe=r'C:\SIMULIA\Commands\abaqus.bat',   # 必须绝对路径
    work_root=r'D:\abqwork',
    cpus_total=16,
    max_concurrent=1,    # 一次只跑一个
    weight=3.0,          # 但多分一些给它，因为它快
)
```

跑之前可以先看分配结果，不执行任何东西：

```python
processor.assignment()    # {'node01': ['j1', 'j3'], 'node02': ['j2', ...]}
```

## 目标机前置条件

管理员 PowerShell：

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service -Name sshd -StartupType Automatic
Get-NetFirewallRule -Name *OpenSSH* | Select Name,Enabled,Profile   # 需对当前活动配置文件启用
Get-ChildItem 'C:\','D:\' -Filter abaqus.bat -Recurse -Depth 5 -ErrorAction SilentlyContinue
```

安装 paramiko：`pip install 'ABQflow[remote]'`（或 `pixi add paramiko`）。
不装也不影响 `import ABQflow` —— 它是惰性导入的。

### 四个会真的踩到的坑

1. **`abaqus_exe` 必须写绝对路径。** 非交互 SSH 会话只继承机器级（HKLM）和用户级
   （HKCU）环境变量，**不含**登录 shell profile 添加的内容；而安装器常只把
   `abaqus.bat` 加到安装者的 PATH 上。
2. **License 环境变量必须是 Machine 级**，否则 RDP 里一切正常、SSH 下失败：
   `[Environment]::GetEnvironmentVariable('ABAQUSLM_LICENSE_FILE','Machine')`
3. **IPv6 字面量要写裸的**（`2001:db8::1`，不加方括号）。link-local 需要**数字**
   zone index（`fe80::1%12`），不是 Linux 的 `%eth0`。
4. **微软账户绑定的登录**做 SSH 密码认证不可靠，建议用专用本地账户。

## 与 Remote Desktop 共存

不冲突。SSH 会话不占用 Windows 的交互式会话槽，作业以脱离方式在 session 0 运行，
既能扛住 SSH 断开，也不受 RDP 注销影响。

两点实际影响：后台作业会和你 RDP 里的操作**争 CPU 和 license token**
（`reserve_cores` 就是留给这个的）；另外它们不在你的 RDP 会话里，
任务管理器要勾"显示所有用户的进程"才看得到。

**不要在作业运行期间用 CAE 打开正在写的 `.odb`** —— `.lck` 存在时文件被锁住。

## 混合 Abaqus 版本

机器池里可以混用不同 Abaqus 版本。注意 **Abaqus 2022 及更早版本的 `abaqus python`
是 Python 2.7**，所以 hook 脚本必须同时兼容 Py2.7 和 Py3。
`hookkit.py` 本身是兼容的（有 `test/unit/test_hookkit_py27.py` 的 AST 扫描守着），
但**你自己写的 hook 脚本**也得注意：不能用 f-string、不能用
`open(..., encoding=...)`。

## 结果去向

- `.sta` / `.msg` / `.dat` / `.log` / sidecar `.csv` → 回传到本地
  `base_output_dir/<job_name>/`，然后由**未改动的** `diagnose()` 判定
- `.odb` → 留在远端。要回传就设 `HostSpec.fetch_odb=True`
- 远端工作目录默认**不清理**（`cleanup='never'`）—— 目录被删掉的远程作业没法调试

## 运行

```powershell
$env:ABQFLOW_NODE01_PASSWORD="..."
$env:ABQFLOW_NODE02_PASSWORD="..."
pixi run python examples/08_RemoteSubmission/run_remote_batch.py
```

把要提交的 INP 放进 `inp_files/`。
