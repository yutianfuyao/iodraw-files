# 燕云十六声自动按键

本工具通过屏幕截图识别指定区域的黄色光效，并使用原型运动检测观察敌方区域的动作变化。它不读取、写入或注入游戏进程。

## 安装与启动

### 运行环境

- Windows 10/11
- Python 3.11 或更高版本
- 游戏进程名称必须为 `yysls.exe`
- 游戏和脚本建议使用相同权限启动；如果游戏以管理员身份运行，脚本也请以管理员身份运行

首次安装在 Windows PowerShell 中执行（示例，yysls是我自己命令，下载全部文件请自行命名）：

```powershell
cd C:\Users\35342\OneDrive\Desktop\yysls
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.example.json config.json -ErrorAction SilentlyContinue
```

```

如果 PowerShell 禁止激活虚拟环境，可以不激活，直接使用虚拟环境里的 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

之后双击运行 `main.py`，或在已激活虚拟环境的 PowerShell 中执行：

```powershell
python main.py
```

如果需要把 PowerShell 输出同时保存下来，使用：

```powershell
& .\.venv\Scripts\python.exe .\main.py 2>&1 | Tee-Object -FilePath .\powershell-run.log
```

识别、按键和异常日志始终写入 `automation.log`。看到 `Key down` 只表示识别逻辑触发；
新版还会记录 `SendInput ok` 或 `SendInput failed`，应以该结果判断 Windows 是否真正接收了模拟按键。

程序会打开控制面板，而不是直接开始检测。

### 每次运行

1. 启动游戏，确认任务管理器中能看到 `yysls.exe`。
2. 启动脚本：

   ```powershell
   cd C:\Users\35342\OneDrive\Desktop\yysls
   .\.venv\Scripts\Activate.ps1
   python main.py
   ```

3. 点击 **配置脚本**，框选黄光区域和单个敌人模型区域，设置按键保持时间，然后点击 **保存配置**。
4. 首次校准时关闭 **启用按键输出**，可勾选 **显示 OpenCV 调试窗口** 检查识别结果。
5. 确认识别正常后，再次进入配置界面勾选 **启用按键输出** 并保存。
6. 点击 **启动脚本**。控制面板会最小化，脚本在后台待命。
7. 将游戏窗口切到前台后，脚本才会开始截图、识别并发送按键；切出游戏后会自动暂停。

不显示控制台窗口时，可以使用虚拟环境中的 `pythonw.exe` 启动：

```powershell
.\.venv\Scripts\pythonw.exe main.py
```

使用 `pythonw.exe` 时请通过 GUI 的 **关闭脚本** 或 `F8` 停止，日志仍写入 `automation.log`。

## 控制面板

主界面包括：

- **启动脚本**：立即转入后台待命并最小化控制面板，不弹出前台提醒。
- **关闭脚本**：立即请求停止检测并释放 `Shift`、`E`。
- **配置脚本**：打开区域配置界面。

点击 **启动脚本** 不要求游戏当时已经在前台；脚本会后台等待 `yysls.exe`。未检测到游戏或游戏不在前台时不会弹窗，也不会发送按键。

状态区会显示游戏进程、脚本运行状态和最新的识别值。后台待命时不会截图、识别或按键；只有当前前台窗口所属进程是 `yysls.exe` 时，才自动开始截图、识别并发送按键。切出游戏、最小化游戏或游戏退出时会自动暂停，切回游戏后恢复。

## 配置

点击 **配置脚本** 后，可为两个区域选择任一种方式：

- 直接填写左、上、宽、高的物理屏幕像素。
- 点击 **截屏框选**，在全屏截图上拖拽区域。松开鼠标即可保存选择，按 `Esc` 取消。

黄色区域应只覆盖黄光出现的范围；敌人区域尽量只覆盖单个敌人模型，避开 HUD 和其他敌人。点击 **保存配置** 后写入 [config.json](config.json)。

如需进一步调节识别阈值，可直接编辑 `config.json` 中的 `yellow_hsv_lower`、`yellow_hsv_upper`、`trigger_ratio` 与 `yellow_min_pixels`。

## YOLOv8 识别模式

当前推荐的高精度模式是 `yolo_hybrid`：YOLOv8 负责识别敌人和攻击姿态，黄光仍由颜色检测负责，避免把短暂光效完全交给目标检测模型。训练完成后，将权重放到 `models/yolov8s_yysls.pt`，并把 `config.json` 改为：

```json
"vision_backend": "yolo_hybrid"
```

模型至少需要两个类别，名称必须与配置一致：`enemy`、`enemy_attack`。可选类别为 `yellow_flash`。只有 `enemy_attack` 置信度连续达到 `attack_stable_frames` 帧，才会进入 E 按键逻辑。

### 采集和训练

先安装 YOLO 依赖：

```powershell
& .\.venv\Scripts\python.exe -m pip install ultralytics
```

使用一次大区域截图采集训练画面：

```powershell
& .\.venv\Scripts\python.exe .\collect_dataset.py `
  --left 650 --top 180 --width 620 --height 620 `
  --fps 10 --output dataset/raw --preview
```

使用标注工具将图片整理为 YOLO 目录结构，并参考 [dataset.yaml.example](dataset.yaml.example) 创建 `dataset/dataset.yaml`。建议按视频划分训练集和验证集，避免相邻帧泄漏。类别应覆盖待机、移动、攻击、受击、镜头转动和特效干扰。

训练 YOLOv8s：

```powershell
& .\.venv\Scripts\python.exe .\train_yolo.py `
  --data dataset/dataset.yaml --base yolov8s.pt `
  --epochs 100 --imgsz 640 --batch 16 --device 0
```

训练完成后复制 `runs/yysls/train/weights/best.pt` 到 `models/yolov8s_yysls.pt`，再启用 `yolo_hybrid`。第一次启用时建议保持 `observe_only: true`，确认日志和调试窗口中的检测框稳定后再打开按键输出。

### 实际游戏画面采集与标注

也可以直接在主界面点击 **数据集训练**，使用内置工作台完成下面流程：选择视频或截图后自动抽帧，逐张拖拽目标框并选择类别，点击“划分训练/验证集”，最后点击“开始训练”。工作台会把临时图片放在 `dataset/studio`，整理后的数据放在 `dataset/images` 和 `dataset/labels`。训练输出会显示在工作台日志中。

内置工作台适合少量或中等规模数据。长视频建议分场景、分批导入，避免一次性导入几万张图片导致标注和磁盘管理困难。

导入时可点击“上传视频/图片”选择一个或多个文件，也可使用“导入文件夹”批量读取该文件夹第一层中的媒体文件。图片路径包含中文也支持。导入过程会在后台执行，完成后在右侧日志显示导入数量；无法读取的视频或图片会显示具体文件名。更新脚本后请关闭旧 GUI 并重新启动，旧窗口不会加载新的导入修复。

1. 启动游戏并进入训练场或普通战斗，把游戏设置为窗口化/无边框窗口。先确认敌人和黄光都位于待检测区域内。
2. 用控制面板配置“单个敌人模型”区域。采集时区域可以稍微放大，确保敌人完整出现，但不要包含大量 HUD 或其他敌人。
3. 按场景分批采集，而不是只录一段画面。建议至少采集：敌人待机、走动、攻击起手、攻击中段、攻击结束、受击、镜头移动、多个特效干扰、无敌人和无黄光画面。
4. 每批采集运行下面的命令，采集结束时在预览窗口按 `Esc`：

```powershell
cd C:\Users\35342\OneDrive\Desktop\yysls
& .\.venv\Scripts\python.exe .\collect_dataset.py `
  --left 650 --top 180 --width 620 --height 620 `
  --fps 8 --output dataset/raw/idle --preview
```

将 `idle` 改为 `attack`、`effects` 等即可分目录采集。每段 30-90 秒通常足够，重点是覆盖不同敌人、距离、地图亮度和画面特效。不要只保留连续的相邻帧，否则验证集会虚高。

5. 使用 CVAT、Label Studio 或 LabelImg 标注 PNG。YOLO 标签使用同名 `.txt` 文件，每行格式为：

```text
class_id x_center y_center width height
```

坐标必须是 0-1 归一化值。类别顺序固定为：

```text
0 enemy
1 enemy_attack
2 yellow_flash
```

标注规则：

- 普通待机/移动帧：给敌人完整身体画框，类别为 `enemy`。
- 明确处于攻击起手或攻击动作的帧：同一个敌人只画一个框，类别为 `enemy_attack`，不要再叠加一个 `enemy` 框。
- 黄光类别可选，框住黄光主体即可；如果主要使用 HSV 黄光检测，也可以不标注 `yellow_flash`。
- 画面中没有目标时保留图片，但可以没有对应 `.txt` 文件，作为负样本。
- 不要把玩家角色、血条、准星、地面特效标成敌人。

6. 将标注后的数据整理成以下结构（图片和标签文件名必须一一对应）：

```text
dataset/
  dataset.yaml
  images/
    train/
    val/
  labels/
    train/
    val/
```

建议按“视频片段”划分训练集和验证集：80% 片段放 `train`，20% 完整片段放 `val`。不要把同一段视频的相邻帧随机拆到两边。

7. 复制示例 YAML 并确认路径：

```powershell
Copy-Item .\dataset.yaml.example .\dataset\dataset.yaml
notepad .\dataset\dataset.yaml
```

如果数据集不在项目目录，修改 `path` 为包含 `images` 和 `labels` 的绝对路径。训练前检查每个类别都有样本，尤其是 `enemy_attack`；攻击样本太少时模型会倾向于把普通动作误判为攻击。

8. 开始训练并观察验证指标：

```powershell
& .\.venv\Scripts\python.exe .\train_yolo.py `
  --data .\dataset\dataset.yaml `
  --base yolov8s.pt --epochs 100 --imgsz 640 --batch 16 --device 0
```

训练结束后重点检查 `runs/yysls/train/` 下的混淆矩阵、验证图片和 `best.pt`。不要只看 mAP：用未参与训练的完整战斗片段测试，记录“误触发 E 次数”和“漏检攻击次数”。如果误触发高，增加移动/镜头转动/特效负样本，提高 `yolo_confidence` 或 `attack_stable_frames`。

当前配置默认启用按键输出 `observe_only: false`，启动前请先完成区域和阈值校准。若要先观察而不发送输入，在配置界面取消勾选“启用按键输出”后保存。需要查看掩码时，可勾选“显示 OpenCV 调试窗口”。

## 控制与保护

- `Esc`：仅在调试窗口处于焦点时退出。
- `F8`：全局紧急停止，并释放 `Shift` 和 `E`。
- 只有 `yysls.exe` 的前台窗口可以接收脚本按键。
- 黄色判定优先于动作判定；黄光存在时不会触发 `E`。
- `shift_cooldown_ms` 与 `e_cooldown_ms` 防止重复按键。
- `key_hold_ms` 控制真实按键时长：脚本会按下键、保持指定毫秒、再松开，不是瞬时点击。默认 `120ms`，可在“配置脚本”界面调整。

### 无效果排查

1. 确认 `automation.log` 中出现 `Started. mode=active`，而不是 `mode=observe`。
2. 确认触发时同时出现 `SendInput ok`。如果是 `SendInput failed ... last_error=87`，说明旧版输入结构体或旧进程仍在运行，请先关闭脚本，再使用上面的虚拟环境命令重新启动。
3. 如果出现 `ERROR_ACCESS_DENIED (5)`，请让脚本与游戏使用相同权限启动；游戏以管理员身份运行时，脚本也需要管理员身份运行。
4. 只有 `yysls.exe` 是前台窗口时才会发送输入；切出游戏时日志出现 `Blocked queued ... is no longer foreground` 属于正常保护行为。

更新代码后必须关闭旧的 GUI 进程再启动。旧进程会继续使用启动时加载的旧按键实现；如果旧 GUI 是从“管理员 PowerShell”启动的，请也在管理员 PowerShell 中关闭并重新启动，避免普通权限窗口无法结束它。

## 攻击动作识别的当前状态

`color_motion` 模式中的 `MotionAttackDetector` 只是原型级帧差检测，不能可靠区分攻击、走动、镜头移动、特效或遮挡。`yolo_hybrid` 模式通过训练的 `enemy_attack` 类改善这一点，但仍需要覆盖实际游戏场景的数据集。

主循环与结果接口已独立。后续可将 `MotionAttackDetector` 替换为读取连续帧的 ONNX 攻击分类器，而无需改动黄色检测、按键优先级、冷却或前台窗口保护。
