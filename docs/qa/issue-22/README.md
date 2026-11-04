# Issue #22 · 点「开始捕获」就 SIGSEGV —— 复现、根因、修复、验证

老板 2026-11-04 真机验收报的必现崩溃（2/2，`make check` 236 项全绿也拦不住）。
本目录放的是**这台机器上真跑出来的原始证据**，不是读代码推出来的说法。

## 一句话结论

**崩因**：本进程**第一次碰 CoreMedia 桥**这件事，原先发生在 ScreenCaptureKit **自己创建**的那条
音频队列线程上（崩溃报告里的线程名 `com.screenCaptureKit.audioSampleHandlerQueue`）。
`import CoreMedia` 会跑它自己的 `_setup()`：加载框架镜像、注册包装类与函数符号，这过程要创建
ObjC 对象；那条线程不是 Python 建的。于是紧接着的第一次值转换（PyObjC 把 CoreMedia 的返回值
变成 Python 对象）对着一个坏指针发消息 → `EXC_BAD_ACCESS / KERN_INVALID_ADDRESS at 0x10` → 段错误。

**承重的那条修法**（差分实验重算过，见下面「两条措施各管什么」）：**把这次加载挪回开流之前的主线程**
（`sck.load_coremedia()`，`SCKSession.start()` 第一步；加载失败直接用人话报错，绝不留半启动的流）。
另外两条：delegate 回调套 `objc.autorelease_pool()`（那条线程的常规卫生，**不是主闸**）、
解析链**逐步判空 + 判返回码 + 判格式**并计数吭声（`drop_report()`）。

**顺带查出来的第二件事**：真机送来的 PCM 是**分声道连续存放（planar）**，代码一直按交错读——
读错不崩也不报错，但送进识别的声音是每个声道被 2 倍抽取的劣化信号。已按双判据分流（ADR 21）。

## 崩溃的第一现场（老板给的那两份 .ips）

`logs/crash-frames-before-fix.txt`（由 `tools/read_crash_report.py` 从
`~/Library/Logs/DiagnosticReports/python3.12-2026-11-04-12*.ips` 解析，可重跑）：

```
崩溃线程 #7：queue='com.screenCaptureKit.audioSampleHandlerQueue'
  #0  libobjcMsgSend.dylib        objc_msgSend
  #1  _objc.cpython-312-darwin.so id_to_python:3403
  #3  _objc.cpython-312-darwin.so pythonify_c_value:2192
  #5  _objc.cpython-312-darwin.so PyObjCFFI_BuildResult:4143   ← 把 CoreMedia 返回值转成 Python 时
  #7  python3.12                  _PyEval_EvalFrameDefault      ← 我们的 Python 回调
  #10 _objc.cpython-312-darwin.so method_stub:1645              ← PyObjC 调 delegate 方法
  #13 ScreenCaptureKit            __59-[SCStream(SCContentSharing) startRemoteAudioReceiveQueue:]_block_invoke
异常：EXC_BAD_ACCESS / SIGSEGV / KERN_INVALID_ADDRESS at 0x0000000000000010
```

第二份报告崩在 `-[NSIndirectTaggedPointerString getCharacters:range:]`（地址 `0x80a35e6661`），
同一类「把坏指针当 ObjC 对象发消息」，只是走字符串那条转换路径。

## 要求 1：先复现

```bash
# A. 与老板手工验收同款：真实窗口 → 点真实「开始捕获」按钮 → say 放音（实时字幕开着）
caffeinate -di -u -t 300 .venv/bin/python docs/qa/issue-22/tools/run_capture.py --sentences 3 --subtitles

# B. 只压捕获链：反复「起真流 → 放一句 → 停流」
caffeinate -di -u -t 900 .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 14

# C. 想看「改之前为什么会崩」——用同一份入库脚本，绕开开流前的主线程预热
caffeinate -di -u -t 200 .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 6 --no-prewarm
```

本机跑出来的原始输出（`logs/stress-baseline-0/1/2.log`、`logs/repro-with-no-prewarm.log`、
`logs/app-attempts-before-fix.log`）：

```
=== C（入库脚本 --no-prewarm，2026-11-04 16:26）===
[stress] HEAD=23e14c7 prewarm=False
[stress] --no-prewarm：开流前不预热，首帧在回调线程首次触碰 CoreMedia
sys:1: ObjCPointerWarning: PyObjCPointer created: at 0x7757d68000 of type ^{opaqueCMSampleBuffer=}
Fatal Python error: Segmentation fault

Current thread 0x000000016fa2f000 (most recent call first):
  File ".../app/capture/sck.py", line 288 in extract_audio_samples
  File ".../app/capture/sck_session.py", line 85 in stream_didOutputSampleBuffer_ofType_
exit=139（SIGSEGV）
```

`extract_audio_samples` 那一行就是 `CMSampleBufferGetFormatDescription(...)`——**与老板崩溃报告的
栈顶是同一处调用**。（说明：`logs/crash-frames-before-fix.txt` 里的 `KERN_INVALID_ADDRESS at 0x10`
与线程名来自系统的 .ips；faulthandler 那份输出只有 Python 栈，两者不是一回事，别混着引用。）

**复现率（修复前）**：

| 跑法 | 结果 |
|---|---|
| B 压力脚本（当时是 `/tmp/stress_sck.py`，与入库版同源）×3 个进程 + 入库版 `--no-prewarm` ×3 个进程 | **改前 3/3 在第 1 轮崩**；入库版绕开预热后 3 个进程里 **2 个崩**、1 个被兜底闸拦住（`logs/stress-baseline-0/1/2.log`、`logs/variant-matrix.log`） |
| A 手工同款 ×5 | **5 个进程崩 1 个**（`logs/app-attempts-before-fix.log` attempt 3；另外 4 次各跑了 930~943 块没事） |
| 老板真机（点按钮 + 放音） | 2/2 崩 |
| C 入库脚本 `--no-prewarm`（改后代码上重现旧路径）×3 | 3 个进程 **2 个崩**、1 个被兜底闸拦住（跳 1 帧并告警） |

> 三个别踩的坑：① 屏幕熄灭/锁屏时 `SCShareableContent` 返回 **0 个显示器**，`start()` 报
> 「未找到活动的显示器」——不是授权问题，命令前挂 `caffeinate -di -u` 即可；② 崩的窗口是
> **每个进程只有第一次**（首帧加载），所以判别看**进程数**，稳态跑了多少帧对这个 bug 没有判别力；
> ③ 崩的时刻是「开流之后的最初几帧」，`say` 还没开始放也会崩（静音时 SCK 一样发帧）——
> 「一放声音就崩」是老板看到的现象，不是机制。

## 要求 1 + 验收 1 · 修复后同一操作不崩（对照）

| 跑法（都在最终 head 上重跑过） | 修复前 | 修复后 |
|---|---|---|
| B 压力脚本 `--rounds 14` | 3/3 进程首轮崩 | **5 个进程 × 14 轮全部通过**、3635~3666 帧全解析、0 跳帧、exit 0（`logs/stress-after-fix-1/2.log`、`stress-after-fix-toolcheck.log`、`final-stress-14.log`、`stress-after-fix-final.log`） |
| A 手工同款（真窗口真按钮真放音真字幕） | 1/5 崩 | **4 个进程全过**，各收到 944 块（`logs/app-attempt-1/2/3-after-fix.log`、`app-attempt-after-fix.log`） |
| 3 分钟真放音 soak | 跑不到（首帧就崩） | **最终 head 那次全过**（`logs/soak-180s.log`：186 秒 / 9288 块 / 95 行字幕）。更早一次也通过，但日志被后续复跑原地覆盖，只作说明不作证据 |
| C 绕开预热（对照） | —— | 2/3 进程崩 → 证明「预热」是承重的那一条 |

合计（本目录入库日志逐条数得出来，不含故意绕开预热的对照进程）：**16 个真流进程、106 轮起停会话、约 3.9 万个音频块、0 崩溃**。
口径说明：这个 bug 每进程只发生一次首帧加载，所以**判别单位是进程数**；
稳态跑了多少帧只能证明「后面不崩」，不能证明「首帧安全」。

## 两条措施各管什么（差分实验，一次只改一个变量；结论：只有一条是防线）

原始输出 `logs/variant-matrix.log`（入库脚本 `--no-prewarm` / `--no-pool`，每个变体 3 个进程 × 6 轮）：

| 变体 | 结果 | 结论 |
|---|---|---|
| 生产代码（预热 + 池） | 3/3 进程不崩、18 轮 0 跳帧 | —— |
| 只拆池（`--no-pool`，保留预热） | 3/3 进程不崩 | **预热是主闸** |
| 只绕开预热（`--no-prewarm`，保留池） | 2/3 进程首轮崩 | **池不是防线**，只是那条线程的常规卫生 |
| 早先修复前的临时脚本（无池、无预热） | 3/3 崩 | 与上面对照一致 |

**没查清的部分照实说**：PyObjC 内部到底是哪一步坏掉没有定位。缺池时 Foundation 的常规表现是
**泄漏并往 stderr 打 `_NSAutoreleaseNoPool`**，而本机 25 份日志里这个串 **0 命中**，所以「缺池导致
对象过早回收」只是猜测，不当结论写。此外池**罩不到入参**的 ObjC→Python 转换（那一步在 PyObjC 的
`method_stub` 里，早于我们的函数体），主闸只能是「别让首次加载落在那条线程上」。

## 要求 2：空值、返回码与格式防护（不再静默丢帧）

`app/capture/sck.py::extract_audio_samples` 现在的判据（每条都有稳定原因码 + 人话文案，首次就
WARNING、同类每 100 帧再报一次、计数进 `drop_report()`；限流的键是原因码，数值只进 detail）：

| 拿到的东西 | 不判会怎样 |
|---|---|
| sample buffer 为 `None` | 直接进 CoreMedia 解引用 |
| 桥还没加载 + 当前不是主线程 | 现场加载 = 本票的崩溃现场；现在**拒绝加载**并跳帧告警（原因码 `BRIDGE_MISSING`） |
| 格式描述为 `None` / mediaType 不是 `'soun'` | 拿非音频的格式描述当 ASBD 来源，下一步就是坏指针 |
| ASBD 为 `None` / 形状不认识 | 采样率、声道数取默认值 → 垃圾音频 |
| 采样率或声道数 ≤ 0 | 老代码用 `or 16000`、`or 1` **猜**成合法值继续用；现在跳帧并说明 |
| formatID 不是 `'lpcm'` / 位深不是 32 / 没有浮点标志 | int16 当 float32 解 → 满屏噪音送识别 |
| 声道存放方式的两个判据互相矛盾 | 老代码不判；现在不猜，跳帧告警（`LAYOUT_CONFLICT`）。判据来自**两处独立口径**：flags 的 `IsNonInterleaved` 位（1<<5=32，已用 `CoreAudio.kLinearPCMFormatFlagIsNonInterleaved` 核对）与 `mBytesPerFrame`（planar=4=每声道；交错=4×声道数）。真机两条都指 planar |
| 数据块缓冲为 `None` / 长度 ≤ 0 / 长度不是 `声道×4` 整数倍 | 空指针、0 字节分配、reshape 抛异常 |
| `CMBlockBufferCopyDataBytes` 返回码非 0 | 把没填过的 ctypes 缓冲当音频用 |
| 任何一处桥调用抛 Python 异常 | 老代码整段包 try 但只 `logger.exception`；现在**统一进 `drop_report()` 吭声**（`BRIDGE_CALL_FAILED`，detail 留异常文本） |

**没有用 `try/except` 去「包」段错误**——Python 抓不到 SIGSEGV，包了也没用。上面全部是
在解引用之前就不往下走。老实说：老代码对 `fmt`/`asbd`/`data_buf`/`length`/`OSStatus` **都判了**，
本次补的不是「从无到有」，而是**吭声 + 计数 + 不猜 + 补格式与线程两道判据**。
另外要写清可见性：这些告警目前**只有从终端跑（`make run`）才看得见**——应用侧没有任何日志配置、
打包又是 `console=False`，发布成 .app 后等于没吭（TD-13 / TD-18）。

## 顺带查出来的第二处错：声道存放方式读反了

真机 ASBD（`logs/frame-format-measured.txt`，`tools/probe_frame_format.py` 可重跑，最近一次
**247 帧全解析、0 跳帧**——新加的格式判据没有误伤真帧）：

```
ASBD = [16000.0, 1819304813('lpcm'), 41, 4, 1, 4, 2, 32, 0]
mediaType = 1936684398('soun')   块缓冲 2560 字节/帧   CMSampleBufferGetNumSamples = 320
```

flags=41 按 CoreAudio 常量逐位拆是 `IsFloat(1) | IsPacked(8) | IsNonInterleaved(32)`，
`mBytesPerFrame=4` 是**每声道**口径——即**分声道连续存放（planar）**。判定不靠读常量表猜，
是靠放一段「左声道 1000Hz、右声道 3000Hz」的立体声实测的
（`logs/channel-layout-measured.txt`，`tools/probe_channel_layout.py` 可重跑）：

```
帧 1: 前半峰值 [1000, …]        ← 交错的话这里应该同时出现 3000
      后半峰值 [3000, …]        ← 交错的话这里应该同时出现 1000
```

交错读法会得到偶/奇下标各自都是混叠频率（同一份日志里就有）——**不崩、不报错，只是声音被
2 倍抽取（混叠 + 时长减半）**。这就是老板点的「非预期格式」那一条的实际形态。
现在按双判据分流：planar 走 `_planar_to_mono`，交错走原来的 `_interleaved_to_mono`，吵架就跳帧吭声。

## 要求 3：真链路测试

`tests/test_sck_real_buffer.py` 用 CoreMedia 构造函数造**真** `CMSampleBufferRef`
（`CMBlockBufferCreateWithMemoryBlock` + `CMBlockBufferReplaceDataBytes` +
`CMAudioFormatDescriptionCreate` + `CMSampleBufferCreateReady`，每一步的 OSStatus 都断言为 0），
驱动生产的 `extract_audio_samples` 与**真 delegate 类**，覆盖老板点名的三种输入：

- **正常**：float32 双声道 planar（真机形状）→ 单声道样本值逐个对得上（不只验长度）；
- **空数据**：真 buffer 但没有数据块 → 跳帧 + 人话原因；
- **非预期格式**：真 int16 buffer → 跳帧并写明「位深不是 32 位浮点」，绝不返回垃圾；
- 另有：空 sample buffer、拷贝返回码非 0（OSStatus 进 detail）、未预热时**拒绝**在工作线程现场加载、
  热路径**一个 import 都不执行**（把 `builtins.__import__` 换成会炸的版本再解一帧）、
  桥调用抛异常也要进记账、planar 与交错两种读法各钉一条（左右声道取不同值，读错立即红）、
  delegate 每帧进出 autorelease pool 一次（第二条防线不再是零覆盖）、真 delegate 收到视频帧不误报、
  预热之后从工作线程喂 200 帧真 buffer、`SCKSession.start()` 的调用顺序必须是
  `load_coremedia → addStreamOutput → startCapture`（顺序断言，直接把根因钉住）。

`make check`：**254 项全绿**（基线 236，只增不减，既有断言一条没改）。
假件也升级了：`tests/test_assembly_layers.py` 的 `_FakeCoreMedia` 原来给 7 元素假元组
（formatID=0、位深=0，与真机不符），现在给真机口径的 9 元素自洽形状（flags=41 + 每声道 4 字节）。
`pr-check.yml` 的 runner 是 `macos-14`，CoreMedia 是项目依赖，构造真 buffer 不需要屏幕录制授权 →
**这条真链路在 CI 里也真的在跑**（CI 日志里 254 项全过）。

## 要求 4 + 验收 3：3 分钟真放音 soak（真识别 + 真翻译）

`logs/soak-180s.log`（31 句 `say`，句间 1.2 秒静音；识别走 siliconflow SenseVoiceSmall，
翻译走本机 127.0.0.1:3425）：

```
2026-11-04 16:54:25,552 INFO repro 点击「开始捕获」
2026-11-04 16:54:26,076 INFO app.capture.sck_session SCK 流已启动
2026-11-04 16:57:51,775 INFO repro soak 结束 186s：收到音频块 9288 个，运行中=True，解析跳帧=无
2026-11-04 16:57:53,830 INFO repro 事件循环退出 rc=0（139=SIGSEGV，段错误）
```

**186 秒、31 句、9288 个音频块、95 条字幕日志（51 条不同原文、其中 44 条已带译文）、
0 崩溃、0 跳帧、识别服务 0 次超时。**

```
16:57:41  原文='The quick brown fox jumps over the lazy dog.'  译文='敏捷的棕色狐狸跳过了懒狗。'
16:57:46  原文='Live caption turns whatever the computer plays into subt.'
          译文='实时字幕能把电脑播放的任何内容转成字幕。'
```

说明：日志行数 ≠ 句子数（同句会先出草稿再定稿）；本目录另有一份更早的 soak（15:32，
9288 块 / 74 行字幕），它的数字随代码改动重跑被刷新过，**以现在这份（16:54，最终 head）为准**。

## 单元测试的天花板（已写进 docs/architecture.md）

单测能覆盖「真 CoreMedia 对象 + 解析逻辑 + 真 delegate selector」，**覆盖不到**
「SCK 在自己那条 GCD 队列线程上派发第一帧」——那需要真流、真放音、真授权。
这就是 236 项全绿还必现的原因。所以这条链的验收分两层：`make check` 挡解析与调用顺序的
回归；本目录脚本挡线程与加载这一层。改到这条链，两层都要跑。

## 残留（已记技术债，不在本票范围）

- **TD-13 / TD-18**：跳帧只进日志，界面上看不出来；而且**应用侧没有日志落点**（无 logging 配置 +
  `console=False`），发布成 .app 后人话告警等于没吭——架构文档写的「本地文件日志 10MB 滚动」其实没实现。
- **TD-14**：planar 已判定并修好；但台账里 #5/#6/#9/#10 记过的字幕质量与延迟中位数是**旧读法**
  （交错）下量的，与新读法不可比，下次动识别链前要用本目录的 soak 工具重测。
- **TD-15**：屏幕熄灭/锁屏时 `SCShareableContent` 返回 0 显示器，文案「未找到活动的显示器」容易被当成授权问题。
- **TD-16**：本目录的诊断脚本跑完打印之后偶发被 SIGKILL（退出码 137），而应用本体走 `app.quit()`
  退出码恒为 0——「不崩」不等于「teardown 干净」，拿退出码当验收判据前先分类。
- **TD-17**：`objc.autorelease_pool()` 只包了音频 delegate 回调这一条路径；SCK 的完成处理器回调
  （`on_content` / `on_started` / stop）同样在非 Python 线程上收 ObjC 对象，未套池。

## 怎么重跑这套证据

```bash
# 屏幕别熄（熄了 SCK 报 0 显示器，白跑）
caffeinate -di -u -t 1500 bash -c '
  .venv/bin/python docs/qa/issue-22/tools/probe_frame_format.py        # 真帧 ASBD/块缓冲/不伤真帧
  .venv/bin/python docs/qa/issue-22/tools/probe_channel_layout.py      # 声道存放方式（planar）
  .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 14
  .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 6 --no-prewarm   # 会 SIGSEGV，这是对照
  .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 6 --no-pool      # 证明预热才是主闸
  .venv/bin/python docs/qa/issue-22/tools/run_capture.py --sentences 3 --subtitles     # 老板手工同款三步
  .venv/bin/python docs/qa/issue-22/tools/run_capture.py --soak 180 --subtitles        # 3 分钟 soak
'
.venv/bin/python docs/qa/issue-22/tools/read_crash_report.py ~/Library/Logs/DiagnosticReports/python3.12-2026-11-04-12*.ips
```
