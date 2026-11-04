# Issue #22 · 点「开始捕获」放音即 SIGSEGV —— 复现、根因、修复、验证

老板 2026-11-04 真机报的必现崩溃（2/2，`make check` 236 项全绿也拦不住）。
这个目录放的是**这台机器上跑出来的原始证据**，不是读代码推出来的说法。

## 一句话结论

**根因**：CoreMedia 的 PyObjC 桥是**懒加载**的——第一次碰 `CoreMedia.某函数` 才真正去
加载框架、注册类型与包装对象，而这过程会产生 autoreleased 的 ObjC 对象。这活儿原先
发生在 `extract_audio_samples()` 里，也就是跑在 **ScreenCaptureKit 自己创建的那条音频
队列线程**上（崩溃报告里的 `com.screenCaptureKit.audioSampleHandlerQueue`）。那条线程
不是 Python 建的、也没有 ObjC autorelease pool，首次加载造出来的桥对象生命周期不成立；
于是 PyObjC 在把 CoreMedia 返回值转成 Python 时对着一个坏指针发消息：
`EXC_BAD_ACCESS / KERN_INVALID_ADDRESS at 0x10` → 段错误。
**崩的总是第一帧**（第二帧起桥已经加载完，所以症状是「一放声音立刻崩」）。

**修法**（都在崩溃链上，没碰断句/草稿/延迟）：

1. `sck.load_coremedia()`：主线程、开流之前把 CoreMedia 的取数据函数加载并缓存好；
   `SCKSession.start()` 第一步就调它，加载失败直接抛人话错误，绝不留半启动的流。
2. delegate 的整段回调包进 `objc.autorelease_pool()`（SCK 那条队列线程上的 ObjC 对象必须有池）。
3. 解析链**逐步判空 + 判返回码 + 判格式**，取不到就跳帧并留人话告警与计数
   （`sck.drop_report()`），不许「静默丢帧还不吭声」。

## 崩溃报告的第一现场

`logs/crash-frames-before-fix.txt`（`tools/read_crash_report.py` 生成，可重跑）。
两份 .ips 是同一条线程、同一段栈：

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

第二份报告崩在 `-[NSIndirectTaggedPointerString getCharacters:range:]`（地址
`0x80a35e6661`）——同样是「把坏指针当 ObjC 对象发消息」，只是走的是字符串那一条转换路径。

## 要求 1：复现

```bash
# A. 与老板手工验收同款：真实窗口 → 点真实「开始捕获」按钮 → say 放音（实时字幕开着）
caffeinate -di -u -t 300 .venv/bin/python docs/qa/issue-22/tools/run_capture.py --sentences 3 --subtitles

# B. 只压捕获链：反复「起真流 → 放一句 → 停流」，把首帧路径打死
caffeinate -di -u -t 900 .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 14
```

修复前的原始输出（`logs/app-attempts-before-fix.log`、`logs/stress-baseline-1/2.log`；
那两份日志里的栈写的是 `/tmp/stress_sck.py`——就是 `stress_sessions.py` 的前身）：

```
=== attempt 3 (subtitles ON) ===
2026-11-04 13:23:37,032 INFO repro 点击「开始捕获」
2026-11-04 13:23:37,534 INFO app.capture.sck_session SCK 流已启动
2026-11-04 13:23:37,538 INFO repro 点击返回，按钮文字='停止'
sys:1: ObjCPointerWarning: PyObjCPointer created: at 0x787d746d80 of type ^{opaqueCMSampleBuffer=}
Fatal Python error: Segmentation fault

Current thread 0x000000016d8d3000 (most recent call first):
  File ".../app/capture/sck.py", line 93 in extract_audio_samples
  File ".../app/capture/sck_session.py", line 73 in stream_didOutputSampleBuffer_ofType_
=== attempt 3 exit=139 ===
```

`line 93` 就是 `fmt = CoreMedia.CMSampleBufferGetFormatDescription(sample_buffer)`，
与系统崩溃报告的栈顶一致。**修复前：B 脚本 3/3 次都在第 1 轮第一帧崩（exit 139）；
A 手工同款 5 次崩 1 次（其余 4 次运气好没撞上）；老板真机 2/2 崩。**

> 复现坑（写下来免得下次再撞）：屏幕熄灭或锁屏时 `SCShareableContent` 返回 **0 个显示器**，
> `start()` 会报「未找到活动的显示器」——不是授权问题。命令前挂 `caffeinate -di -u` 即可。

## 验收 1 · 要求 1 的对照：修复前后同一操作

| 跑法 | 修复前 | 修复后 |
|---|---|---|
| B 压力脚本 14 轮 ×2 | 首轮即 SIGSEGV（exit 139） | 28/28 轮全过，exit 0（`logs/stress-after-fix-1/2.log`） |
| B 压力脚本（入库版重跑） | 同上 | 14/14 轮、3636 帧全部解析成功、0 跳帧、exit 0（`logs/stress-after-fix-toolcheck.log`） |
| B 压力脚本（**最后一笔改动后在最终 head 复跑**） | 同上 | 14/14 轮、3666 帧全解析、0 跳帧、exit 0（`logs/final-stress-14.log`） |

修复后合计 **61 次真流会话、约 3.4 万帧音频块、0 崩溃**（本目录日志里能数出来：14 轮 ×4 次 + 手工同款 3 次 + soak 2 次 = 61 次会话）。
| A 手工同款 ×3 | 1/5 崩 | 3/3 不崩，各收到 944 块（`logs/app-attempt-{1,2,3}-after-fix.log`） |
| 差分实验（同脚本，一次只改一个变量） | 回调里首次加载 → 3/3 崩 | 主线程只 `import CoreMedia`：0/6 轮；主线程 import+解析函数：0/36 轮；不改加载、只给回调加 autorelease pool：0/28 轮 |

两条缓解各自都能压住，说明它们是**同一处问题的两面**（加载发生在错的线程 + 那条线程没有池），
所以都进了代码。

## 要求 4 + 验收 3：3 分钟真放音 soak（真识别 + 真翻译）

两份 soak 日志：修复后立刻跑过一次（186 秒 / 9299 块 / 104 条字幕），最后一笔代码改动之后**在最终 head 上又复跑一遍**——仓库里保存的是后者：


```
2026-11-04 15:32:12,537 INFO repro 点击「开始捕获」
2026-11-04 15:32:13,050 INFO app.capture.sck_session SCK 流已启动
2026-11-04 15:35:18,843 INFO repro soak 结束 186s：收到音频块 9288 个，运行中=True，解析跳帧=无
2026-11-04 15:35:20,892 INFO repro 事件循环退出 rc=0（139=SIGSEGV，段错误）
```

**186 秒、31 句 `say` 语音（句间 1.2 秒静音）、9288 个音频块、74 条字幕、0 崩溃、0 跳帧。**
（字幕条数随断句与真服务的响应时机浮动，两轮分别是 104 与 74；关键是「非 0」且没有崩溃/跳帧。）
字幕抽样（原文 / 译文都真的出来了）：

```
15:32:23  原文='The quick brown fox jumps over the lazy dogg.'  译文='敏捷的棕色狐狸跳过了懒狗。'
15:32:25  原文='My cap turns whatever the computer plays.'  译文='我的帽子能转换电脑播放的任何内容。'
15:32:27  原文='看一下。'  译文='Take a look.'
```

## 要求 2：空值与返回码防护（不再静默丢帧）

`app/capture/sck.py::extract_audio_samples` 逐步判（老实说：`fmt`/`asbd`/`data_buf`/`length`/`OSStatus` 老代码**都判了**，缺的是判完一律 `return None` 不出声、不计数，外加两处 `or 16000`/`or 1` 的猜测与「格式完全不判」），每条原因都写成人话进日志（首次就
WARNING，同类每 100 帧再报一次）并计入 `drop_report()`：

| 拿到的东西 | 判据 → 不判会怎样 |
|---|---|
| sample buffer 为 `None` | 直接进 CoreMedia 解引用 |
| 格式描述为 `None` | 下一步把空指针当 ASBD 来源 |
| ASBD 为 `None` / 形状不认识 | 采样率、声道数取默认值 → 垃圾音频 |
| formatID 不是 `'lpcm'`、位深不是 32、没有浮点标志、非交错 | int16 当 float32 解 → 满屏噪音送识别 |
| 数据块缓冲为 `None` | 空指针 |
| 数据长度 ≤ 0 或不是 `声道×4` 的整数倍 | 分配 0 字节缓冲 / reshape 抛异常 |
| `CMBlockBufferCopyDataBytes` 返回码非 0 | **把没填过的 ctypes 缓冲当音频用** |
| 采样率/声道数 ≤ 0（老代码用 `or 16000`、`or 1` **猜**成合法值） | 猜出来的采样率喂进重采样，声音时长就错了 |
| `CMBlockBufferCopyDataBytes` 的 OSStatus != 0 | 把没填过的缓冲当音频用。**说明**：老代码这条也判了并返回 None，但判完不出声——既不写原因也不计数，日志里查不到丢过多少帧；本次的改动是「吭声 + 计数」，不是「从无到有」 |

**没有用 `try/except` 去「包」段错误**——Python 抓不到 SIGSEGV，包了也没用；上面全部是
在解引用之前就不往下走。留在最后的 `except` 只兜 Python 层异常，同样进 `drop_report()` 吭声。

## 要求 3：真链路测试

`tests/test_sck_real_buffer.py` 用 CoreMedia 构造函数造**真** `CMSampleBufferRef`
（`CMBlockBufferCreateWithMemoryBlock` + `CMBlockBufferReplaceDataBytes` +
`CMAudioFormatDescriptionCreate` + `CMSampleBufferCreateReady`，四步的 OSStatus 都断言为 0），
驱动生产的 `extract_audio_samples` 与**真 delegate 类**，覆盖老板点名的三种输入：

- **正常**：float32 双声道 → 单声道样本值逐个对得上（不只验长度）；
- **空数据**：真 buffer 但没有数据块 → 跳帧 + 人话原因；
- **非预期格式**：真 int16 buffer → 跳帧并写明「位深不是 32 位浮点」，绝不返回垃圾；
- 另有：空 sample buffer、拷贝返回码非 0（OSStatus 进日志）、真 delegate 收到视频帧不误报、
  **预热之后从工作线程喂 200 帧真 buffer**、`SCKSession.start()` 的调用顺序必须是
  `load_coremedia → addStreamOutput → startCapture`（顺序断言，直接把根因钉住）。

顺带把真机实测形状钉成用例并同步了假件：PyObjC 13 把 ASBD 摊平成 **9 元素元组**
`[16000.0, 1819304813('lpcm'), 41, 4, 1, 4, 2, 32, 0]`、块缓冲 2560 字节/帧、
`CMSampleBufferGetNumSamples=320`（原始输出 `logs/frame-format-measured.txt`，
`tools/probe_frame_format.py` 可重跑。最近一次探测里 249 帧真 buffer **全部解析成功、0 跳帧**——这条很重要：新加的格式判据不能把真帧挡掉，只挡真不该吃的东西）。
旧的 `_FakeCoreMedia` 给的是 7 元素假元组（formatID=0、位深=0），与真机不符——已改成真机形状，
**既有断言一条都没改**（48000Hz→1600 样本、满量程音量等仍原样）。

这条链在 CI 里也是真的在跑：`pr-check.yml` 的 runner 是 `macos-14`，CoreMedia 是项目依赖，
构造真 buffer 不需要屏幕录制授权。

## 单元测试的天花板（已写进 docs/architecture.md）

单测能覆盖「真 CoreMedia 对象 + 解析逻辑 + 真 delegate selector」，**覆盖不到**
「SCK 在它自己那条 GCD 队列线程上派发第一帧」——那需要真流、真放音、真授权。
这就是 236 项全绿还必现的原因。所以这条链的验收分两层：`make check` 挡解析与调用顺序的
回归；`docs/qa/issue-22/tools/` 挡线程与加载这一层。改到这条链，两层都要跑。

## 残留（已记技术债，不在本票范围）

- **TD-13**：跳帧只进日志，界面上看不出来（老板侧只会觉得「没声音」）。
- **TD-14**：SCK 报的 ASBD 自相矛盾（2 声道 32 位却写 `mBytesPerFrame=4`），而 `say` 的
  语音左右声道同值 → 交错/非交错两种解释结果一样，**没法用它区分**；需要一段左右声道
  不同的音频才能确定单声道混合是否正确。
- **TD-15**：屏幕熄灭/锁屏时 `SCShareableContent` 返回 0 显示器，现在的文案是
  「未找到活动的显示器，无法捕获声音」，容易被当成授权问题。
- **TD-16**：本目录的诊断脚本跑完之后偶发被 SIGKILL（退出码 137；探针两次都是），
  而应用本体走 `app.quit()` 退出码恒为 0。说明「不崩」不等于「teardown 干净」；
  用退出码当判据时先确认是不是这一类。

## 怎么重跑这套证据

```bash
# 0) 让屏幕别熄（熄了 SCK 报 0 显示器，白跑）
caffeinate -di -u -t 900 bash -c '
  # 1) 量真机帧形状（249 帧全解析、0 跳帧）
  .venv/bin/python docs/qa/issue-22/tools/probe_frame_format.py
  # 2) 首帧路径打死（14 轮起停真流）
  .venv/bin/python docs/qa/issue-22/tools/stress_sessions.py --rounds 14
  # 3) 与老板手工同款：真窗口 + 真按钮 + say 放音 + 真字幕
  .venv/bin/python docs/qa/issue-22/tools/run_capture.py --sentences 3 --subtitles
  # 4) 3 分钟 soak（要真识别/真翻译服务）
  .venv/bin/python docs/qa/issue-22/tools/run_capture.py --soak 180 --subtitles
'
# 读老板给的那两份崩溃报告
.venv/bin/python docs/qa/issue-22/tools/read_crash_report.py ~/Library/Logs/DiagnosticReports/python3.12-2026-11-04-12*.ips
```
