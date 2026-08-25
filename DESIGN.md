# 燕云十六声自动按键脚本 - 设计规范

## 1. 目标与边界

本工具是一个本地 Windows 桌面脚本，只通过屏幕截图分析画面并模拟键盘输入；不读取、写入或注入游戏进程，不修改游戏文件，不访问网络。

目标行为：

1. 持续采集指定屏幕区域（ROI），检测黄色提示光。当黄色像素占比达到阈值时，自动短按 `Shift`。
2. 持续采集指定屏幕区域，识别对手模型是否处于攻击动作。当攻击成立、当前未检测到黄色提示光，并达到动作判定阈值时，自动短按 `E`。

`Shift` 的判定优先级高于 `E`：同一采样周期内只要黄色提示光成立，就不得触发 `E`。

本规范仅定义设计和验收标准，不包含可执行脚本。

## 2. 技术选型

使用 Python 3.11+，适合 Windows 上快速迭代、调试图像掩码与模拟按键。

| 需求 | 建议库 | 原因 |
| --- | --- | --- |
| 高速截图 | `mss` | 可直接抓取固定 ROI，性能通常优于逐帧全屏截图 |
| 图像处理 | `opencv-python`、`numpy` | HSV 阈值、形态学处理、运动分析与调试窗口成熟 |
| 模拟按键 | `pynput` | 支持 Windows 虚拟键盘事件，接口简单 |
| 配置 | 标准库 `dataclasses` / `json` | 参数可保存、可复现，不引入额外服务 |
| 可选动作模型 | `onnxruntime` | 仅在使用训练好的攻击动作分类模型时启用 |

运行环境限定为 Windows。脚本应在管理员权限与游戏权限一致的条件下运行，否则按键事件可能无法被目标窗口接收。

## 3. 配置设计

所有可调参数集中在文件顶部的 `Config` 数据类，启动时打印最终配置。坐标使用物理屏幕像素，避免 Windows DPI 缩放导致 ROI 偏移。

```text
CAPTURE_REGION = {left, top, width, height}  # 黄色检测区域
ATTACK_REGION = {left, top, width, height}   # 敌方模型动作区域
DEBUG_WINDOW = True

YELLOW_HSV_LOWER = (H, S, V)
YELLOW_HSV_UPPER = (H, S, V)
TRIGGER_RATIO = 0.0 ~ 1.0
YELLOW_MIN_PIXELS = integer
YELLOW_STABLE_FRAMES = integer

ATTACK_SCORE_THRESHOLD = 0.0 ~ 1.0
ATTACK_STABLE_FRAMES = integer
ATTACK_COOLDOWN_MS = integer
SHIFT_COOLDOWN_MS = integer
KEY_HOLD_MS = integer
FPS_LIMIT = integer
```

`TRIGGER_RATIO` 是黄色掩码白色像素数除 ROI 总像素数。`YELLOW_MIN_PIXELS` 作为绝对下限，避免极小 ROI 或单点噪声造成误触发。黄色成立条件为：

```text
yellow_ratio >= TRIGGER_RATIO and yellow_pixels >= YELLOW_MIN_PIXELS
```

`YELLOW_STABLE_FRAMES` 和 `ATTACK_STABLE_FRAMES` 都应大于 1，以降低单帧闪烁、压缩噪声和截图撕裂造成的误判。

`KEY_HOLD_MS` 控制模拟按键的持续时间。每次触发必须按顺序执行“KeyDown -> 保持 `KEY_HOLD_MS` 毫秒 -> KeyUp”，默认建议 80 至 180 毫秒，不能实现为单次无持续时间的点击。

## 4. 主循环与优先级

每一帧按以下顺序执行：

```text
截取黄色 ROI 与攻击 ROI
  -> 计算黄色掩码、黄色比例、黄色稳定状态
  -> 计算攻击分数、攻击稳定状态
  -> 黄色已稳定：满足 Shift 冷却则按 Shift，并抑制 E
  -> 否则攻击已稳定：满足 E 冷却则按 E
  -> 绘制调试信息、等待至下一帧
```

按键应使用非阻塞的短按函数：按下，等待 `KEY_HOLD_MS`，释放。主循环不得因等待按键而积压截图；实现时可将按键派发到单一工作线程，或采用极短按键时长并记录冷却时间。

冷却从按键释放后开始计时。若黄色持续出现，`Shift` 只能按第一次并在冷却到期后再次按，不得每帧重复发送。

## 5. 黄色检测

1. 使用 `mss` 抓取 `CAPTURE_REGION`，转换为 BGR，再转换到 HSV。
2. 使用 `cv2.inRange` 根据 `YELLOW_HSV_LOWER`、`YELLOW_HSV_UPPER` 生成二值掩码。
3. 可选地执行一次开运算去除孤立白点，再执行一次闭运算连接连续光效；内核大小必须配置化，默认从 `3x3` 开始。
4. 统计处理后掩码的白色像素和占比，经过连续帧确认后得到 `yellow_active`。

HSV 比 RGB 更适合将黄色的色相与亮度、饱和度分开调节。游戏的后处理、显示器 HDR、夜晚场景或显卡色彩设置都可能改变阈值，因此不应写死通用数值。

## 6. 攻击动作识别

仅凭一张截图无法可靠判断“攻击”。设计采用两层策略，实施时优先使用可验证性更强的模型层：

### 6.1 首选：时序攻击分类器

从 `ATTACK_REGION` 连续采集固定长度的帧序列，例如 8 至 16 帧。对每帧做缩放、归一化后，送入一个专门为该游戏、敌人类型与视角训练或标注过的 ONNX 时序分类器。模型输出 `attack_score`，类别至少包含：`idle`、`attack`、`other_motion`。

模型结果只有在连续 `ATTACK_STABLE_FRAMES` 帧满足：

```text
attack_score >= ATTACK_SCORE_THRESHOLD
```

时才视为攻击。模型文件的路径、输入尺寸、帧数、类别映射与版本号必须写入配置和启动日志。

### 6.2 原型：运动启发式（非最终可靠方案）

在没有训练模型时，可采用帧差、背景建模与稠密/稀疏光流计算敌方 ROI 的运动强度和方向突变。启发式只可用作采集样本、调试 ROI 或早期验证，不应声称能够稳定识别攻击，因为移动镜头、敌人走路、特效、遮挡和多个敌人都会触发类似运动。

启发式输出仍统一为 `attack_score`，这样未来替换为 ONNX 模型不影响主循环、冷却与按键逻辑。

### 6.3 数据与验收要求

建立最少三类录像或帧序列：攻击、待机/走动、闪避/受击/技能特效。验证集必须包含不同光照、距离、敌人、场景和镜头抖动。以攻击召回率和非攻击误触发率分别评估，不能只用“看起来有效”的单次实战测试验收。

## 7. 调试窗口

`DEBUG_WINDOW=True` 时，使用 OpenCV 显示以下窗口或拼接面板：

| 面板 | 内容 |
| --- | --- |
| Yellow ROI | 原始黄色检测区域，叠加 ROI 名称 |
| Yellow Mask | 二值掩码；白色表示被识别为黄色 |
| Attack ROI | 原始敌方模型区域，叠加攻击分数与稳定帧数 |
| Status | `yellow_ratio`、黄色像素数、`yellow_active`、`attack_score`、冷却剩余时间、最近按键 |

调试窗口必须提供一个退出热键，例如 `Esc`，关闭窗口后能保证所有已按下的键被释放。非调试模式不得创建任何 OpenCV 窗口。

## 8. 调参流程

1. 设定尽可能小的黄色 ROI，只覆盖角色身前或技能提示可能出现的范围；不要使用全屏。
2. 将 `DEBUG_WINDOW=True`，在游戏内观察黄色光出现时 `Yellow Mask` 的白色区域与 `yellow_ratio`，同时记录未出现黄光时的最大值。
3. 将 `TRIGGER_RATIO` 设在“无黄光最大值”和“有黄光最小值”之间，并同时设置足够的 `YELLOW_MIN_PIXELS`。
4. 观察连续帧：黄光短暂出现但不应触发时，提高 `YELLOW_STABLE_FRAMES`；响应过慢时小幅降低它。
5. 框定单个敌人模型的 `ATTACK_REGION`，录制攻击与非攻击样本，先验证攻击分类器/启发式的分数分布，再选定 `ATTACK_SCORE_THRESHOLD`。
6. 确认掩码和动作结果后关闭 `DEBUG_WINDOW`，再正式运行。

建议每次只改一个参数并记录场景、参数和结果。若游戏窗口切换分辨率、缩放模式或 UI 布局，必须重新校准坐标和阈值。

## 9. 安全与故障处理

- 仅当 `yysls.exe` 正在运行且其窗口为前台窗口时允许派发按键；失焦、最小化、进程退出或截图失败时停止按键。
- 提供全局紧急停止热键；触发后立即停止循环并释放 `Shift`、`E`。
- 捕获异常后写入本地日志，释放所有键，并以非零状态退出。
- 界面提供“启用按键输出”开关；首次校准时应关闭该开关，确认识别结果后再启用。
- 日志不得记录截图原图，默认只记录时间、测量值、状态切换和按键事件，以控制隐私与文件体积。
- 禁止在冷却未结束时重复派发同一按键；禁止由任何识别异常直接触发按键。

## 10. 验收标准

1. 在设定 ROI 中，黄色掩码与实际黄光位置一致，非黄光场景没有持续白色噪声。
2. 黄色判定成立时只触发 `Shift`，同一时刻不触发 `E`。
3. 黄色不成立且攻击判定稳定成立时，才允许触发 `E`。
4. 短暂单帧噪声、截图异常、失焦、切出游戏和窗口最小化均不会发送按键。
5. 调试模式可实时显示全部关键测量值；关闭调试模式后无调试窗口且主循环仍保持目标帧率。
6. 紧急停止、正常退出和异常退出后，不存在任何逻辑上仍处于按下状态的键。

## 11. 建议的目录结构（后续实现）

```text
xuanhuangame/
  DESIGN.md
  requirements.txt
  config.example.json
  src/
    main.py
    capture.py
    yellow_detector.py
    attack_detector.py
    input_controller.py
    debug_view.py
    safety.py
  models/
    attack_classifier.onnx
  logs/
```

攻击识别模块只暴露 `update(frame, timestamp) -> AttackResult`；黄色识别模块只暴露 `detect(frame) -> YellowResult`。主程序只消费它们的统一结果，不应了解 HSV 参数或模型推理细节。
