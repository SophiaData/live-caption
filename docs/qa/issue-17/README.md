# Issue #17 视觉证据（Before / After）

- `compare_subtitles_v2.png` — 同一场景六格：基线（master 7dc2042）说话期间 / 说完；
  改动后（head 44f9d2d）**开关关闭**说话期间 / 说完；**开关开启**说话期间（暗色草稿）/ 说完（同一行原地替换）。
- `compare_settings_v2.png` — 字幕设置面板三格：④ 基线（master 7dc2042，无该开关）、⑤ 改动后默认勾选（开启）、⑥ 改动后取消勾选（关闭）。标题用应用默认字体，offscreen 下不写死字体名（会渲染成豆腐块）。
- `compare_subtitles.png` / `compare_settings.png` — 第一次交付的两格版本（保留）。

视口/主题/UI 状态：同一 `SubtitleWindow`（960×170，深色底）、同一 `SubtitleSettingsDialog`、
同一段合成音频输入，`QT_QPA_PLATFORM=offscreen` 渲染真实控件后 `grab()`，非示意图。

重新生成（在仓库根目录）：

```
WORK=$(mktemp -d)
mkdir -p "$WORK/baseline" && git archive 7dc2042 | tar -x -C "$WORK/baseline"  # 基线代码树
export LC_OUT="$WORK" LC_BASELINE_TREE="$WORK/baseline" QT_QPA_PLATFORM=offscreen
for s in cap_base cap_settings_base cap_head cap_settings cap_sheet; do
  python docs/qa/issue-17/tools/$s.py
done
# 脚本不写死任何机器目录：仓库根由 Path(__file__).parents[4] 推出，
# 输出目录走 LC_OUT（默认系统临时目录），基线树必须走 LC_BASELINE_TREE（不设就报错退出）。
```

`cap_head.py` 里跑的是真实 `RecognitionPipeline` + 真实断句器（假识别/假翻译只为让文字可控），
所以「说话期间已有暗色草稿」「说完同一行变完整且窗口内字幕条数恒为 2」这两点是代码路径的真实结果。
